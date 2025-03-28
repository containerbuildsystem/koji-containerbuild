"""Koji builder plugin extends Koji to build containers

Acts as a wrapper between Koji and OpenShift builsystem via osbs for building
containers."""

# Copyright (C) 2015-2022  Red Hat, Inc.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
# USA

# Authors:
#       Pavol Babincak <pbabinca@redhat.com>
from __future__ import absolute_import

import os
import os.path
import sys
import logging
import time
import traceback
import signal
import shutil
from distutils.version import LooseVersion

import dockerfile_parse
import jsonschema

# this is present because in some versions of koji, callback functions assume koji.plugin is
# imported
import koji.plugin
import koji
from koji.daemon import SCM, incremental_upload
from koji.tasks import BaseTaskHandler

import osbs
from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.exceptions import OsbsValidationException
from osbs.utils import UserWarningsStore

OSBS_VERSION = osbs.__version__
OSBS_FLATPAK_SUPPORT_VERSION = '0.43'  # based on OSBS 2536f24 released on osbs-0.43
if LooseVersion(OSBS_VERSION) < LooseVersion(OSBS_FLATPAK_SUPPORT_VERSION):
    osbs_flatpak_support = False
else:
    osbs_flatpak_support = True


# List of LABEL identifiers used within Koji. Values doesn't need to correspond
# to actual LABEL names. All these are required unless there exist default
# value (see LABEL_DEFAULT_VALUES).
LABELS = koji.Enum((
    'COMPONENT',
    'VERSION',
    'RELEASE',
    'NAME',
))

# list of labels which has to be explicitly defined with values
LABELS_EXPLICIT_REQUIRED = koji.Enum((
    'NAME',
    'COMPONENT',
))

# list of labels which has to be defined but value may be set via env
LABELS_ENV_REQUIRED = koji.Enum((
    'VERSION',
))

# Mapping between LABEL identifiers within koji and actual LABEL identifiers
# which can be found in Dockerfile. First value is preferred one, others are for
# compatibility purposes.
LABEL_NAME_MAP = {
    'COMPONENT': ('com.redhat.component', 'BZComponent'),
    'VERSION': ('version', 'Version'),
    'RELEASE': ('release', 'Release'),
    'NAME': ('name', 'Name'),
}


METADATA_TAG = "platform:_metadata_"

DEFAULT_CONF_BINARY_SECTION = "default_binary"
DEFAULT_CONF_SOURCE_SECTION = "default_source"

REMOTE_SOURCES_LOGNAME = 'remote-sources'
REMOTE_SOURCES_TASKNAME = 'binary-container-hermeto'


def _concat(iterables):
    return [x for iterable in iterables for x in iterable]


def create_task_response(osbs_result):
    """Create task response from an OSBS result"""
    repositories = osbs_result.get('repositories', [])
    koji_build_id = osbs_result.get('koji_build_id')
    user_warnings = osbs_result.get('user_warnings', [])

    task_response = {
        'repositories': repositories,
        'koji_builds': [koji_build_id] if koji_build_id else [],
    }

    if user_warnings:
        task_response['user_warnings'] = user_warnings

    return task_response


class ContainerError(koji.GenericError):
    """Raised when container creation fails"""
    faultCode = 2001


class ContainerCancelled(koji.GenericError):
    """Raised when container creation is cancelled by OSBS"""
    faultCode = 2002


class My_SCM(SCM):
    def get_component(self):
        component = os.path.basename(self.repository)
        if self.repository.endswith('.git'):
            # If we're referring to a bare repository for the main module,
            # assume we need to do the same for the common module
            component = os.path.basename(self.repository[:-4])
        return component

    def get_git_uri(self):
        scheme = self.scheme
        if '+' in scheme:
            scheme = scheme.split('+')[1]
        git_uri = '%s%s%s' % (scheme, self.host, self.repository)
        return git_uri


class FileWatcher(object):
    """Watch directory for new or changed files which can be iterated on

    Rewritten mock() from Buildroot class of kojid. When modifying keep that in
    mind and after the API looks stable enough try to merge the code back to
    koji.
    """
    def __init__(self, result_dir, logger):
        self._result_dir = result_dir
        self.logger = logger
        self._logs = {}

    def _list_files(self):
        try:
            results = os.listdir(self._result_dir)
        except OSError:
            # will happen when mock hasn't created the resultdir yet
            return

        for fname in results:
            if (fname.endswith('.log') or fname.endswith('.json')) and fname not in self._logs:
                fpath = os.path.join(self._result_dir, fname)
                self._logs[fname] = (None, None, 0, fpath)

    def _reopen_file(self, fname, fd, inode, size, fpath):
        try:
            stat_info = os.stat(fpath)
            if not fd or stat_info.st_ino != inode or stat_info.st_size < size:
                # either a file we haven't opened before, or mock replaced a file we had open with
                # a new file and is writing to it, or truncated the file we're reading,
                # but our fd is pointing to the previous location in the old file
                if fd:
                    self.logger.info('Rereading %s, inode: %s -> %s, size: %s -> %s' %
                                     (fpath, inode, stat_info.st_ino, size, stat_info.st_size))
                    fd.close()
                fd = open(fpath, 'r')
            self._logs[fname] = (fd, stat_info.st_ino, stat_info.st_size, fpath)
        except OSError:
            self.logger.error("The build has been cancelled")
            raise koji.ActionNotAllowed
        except Exception:
            self.logger.error("Error reading mock log: %s", fpath)
            self.logger.error(''.join(traceback.format_exception(*sys.exc_info())))
            return False
        return fd

    def files_to_upload(self):
        self._list_files()

        for (fname, (fd, inode, size, fpath)) in self._logs.items():
            fd = self._reopen_file(fname, fd, inode, size, fpath)
            if fd is False:
                return
            yield (fd, fname)

    def clean(self):
        # pylint: disable=unused-variable
        for (fname, (fd, inode, size, fpath)) in self._logs.items():
            if fd:
                fd.close()


class LabelsWrapper(object):
    def __init__(self, dockerfile_path, logger_name=None, label_overwrites=None):
        self.dockerfile_path = dockerfile_path
        self._setup_logger(logger_name)
        self._parser = None
        self._label_data = {}
        self._label_overwrites = label_overwrites or {}

    def _setup_logger(self, logger_name=None):
        if logger_name:
            dockerfile_parse.parser.logger = logging.getLogger("%s.dockerfile_parse"
                                                               % logger_name)

    def _parse(self):
        self._parser = dockerfile_parse.parser.DockerfileParser(self.dockerfile_path)

    def get_labels(self):
        """returns all labels how they are found in Dockerfile"""
        self._parse()
        return self._parser.labels

    def get_data_labels(self):
        """Subset of labels found in Dockerfile which we are interested in

        returns dict with keys from LABELS and values from Dockerfile as mapped via
        LABEL_NAME_MAP.
        """

        parsed_labels = self.get_labels()
        for label_id in LABELS:
            if label_id not in LABEL_NAME_MAP:
                msg = "Required label '{}' doesn't map to name in Dockerfile".format(label_id)
                raise ContainerError(msg)

            for label_name in LABEL_NAME_MAP[label_id]:
                if label_name in self._label_overwrites:
                    self._label_data[label_id] = self._label_overwrites[label_name]
                    break

                if label_name in parsed_labels:
                    self._label_data[label_id] = parsed_labels[label_name]
                    break
        return self._label_data

    def get_additional_tags(self):
        """Returns a list of additional tags to be applied to an image"""
        tags = []
        dockerfile_dir = os.path.dirname(self.dockerfile_path)
        additional_tags_path = os.path.join(dockerfile_dir, 'additional-tags')
        try:
            with open(additional_tags_path, 'r') as fd:
                for tag in fd:
                    if '-' in tag:
                        continue
                    tags.append(tag.strip())
        except Exception:
            pass

        return tags

    def get_missing_label_ids(self):
        data = self.get_data_labels()
        missing_labels = []
        # check required labels, have to be defined explicitly and not via env
        for label_id in LABELS_EXPLICIT_REQUIRED:
            if not data.get(label_id):
                missing_labels.append(label_id)

        # check for all labels, unless required or default values provided,
        # they should be at least defined (even via env)
        for label_id in LABELS_ENV_REQUIRED:
            if label_id not in data:
                missing_labels.append(label_id)
        return missing_labels

    def get_expected_nvr(self):
        data = self.get_data_labels()
        return "{0}-{1}-{2}".format(data['COMPONENT'], data['VERSION'], data['RELEASE'])

    def format_label(self, label_id):
        """Formats string with user-facing LABEL name and its alternatives"""

        if label_id not in LABEL_NAME_MAP:
            msg = '"{}" does not contain "{}"'.format(LABEL_NAME_MAP, label_id)
            raise ContainerError(msg)

        label_map = LABEL_NAME_MAP[label_id]
        if len(label_map) == 1:
            return label_map[0]
        else:
            return "%s (or %s)" % (label_map[0], " or ".join(label_map[1:]))


class BaseContainerTask(BaseTaskHandler):
    """Common class for BuildContainerTask and BuildSourceContainerTask"""
    def __init__(self, id, method, params, session, options, workdir=None):
        # pylint: disable=redefined-builtin
        BaseTaskHandler.__init__(self, id, method, params, session, options, workdir)
        self._osbs = None
        self._log_handler_added = False
        self.incremental_log_basename = 'osbs-build.log'

    def osbs(self):
        """Handler of OSBS object"""
        if not self._osbs:
            conf_section = None
            if self.method in BuildContainerTask.Methods:
                conf_section = DEFAULT_CONF_BINARY_SECTION
            elif self.method in BuildSourceContainerTask.Methods:
                conf_section = DEFAULT_CONF_SOURCE_SECTION

            os_conf = Configuration(conf_section=conf_section)
            self._osbs = OSBS(os_conf)
            if not self._osbs:
                msg = 'Could not successfully instantiate `osbs`'
                raise ContainerError(msg)
            log_level = logging.DEBUG if os_conf.get_verbosity() else logging.INFO
            self.setup_osbs_logging(level=log_level)

        return self._osbs

    def setup_osbs_logging(self, level=logging.INFO):
        # Setting handler more than once will cause duplicated log lines.
        # Log handler will persist in child process.
        if not self._log_handler_added:
            osbs_logger = logging.getLogger(osbs.__name__)
            osbs_logger.setLevel(level)
            log_file = os.path.join(self.resultdir(), 'osbs-client.log')
            handler = logging.FileHandler(filename=log_file)
            # process (PID) is useful because a buildContainer task forks main process
            formatter = logging.Formatter(
                '%(asctime)s - %(process)d - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            osbs_logger.addHandler(handler)
            # let's log also koji-containerbuild log messages into osbs-client log
            self.logger.addHandler(handler)

            self._log_handler_added = True

    def getUploadPath(self):
        """Get the path that should be used when uploading files to
        the hub."""
        return koji.pathinfo.taskrelpath(self.id)

    def resultdir(self):
        path = os.path.join(self.workdir, 'osbslogs')
        if not os.path.exists(path):
            os.makedirs(path)
        return path

    def _incremental_upload_logs(self, child_pid=None):
        resultdir = self.resultdir()
        uploadpath = self.getUploadPath()
        watcher = FileWatcher(resultdir, logger=self.logger)
        finished = False
        try:
            while not finished:
                if child_pid is None:
                    finished = True
                else:
                    time.sleep(1)
                    status = os.waitpid(child_pid, os.WNOHANG)
                    if status[0] != 0:
                        finished = True

                for result in watcher.files_to_upload():
                    if result is False:
                        return
                    (fd, fname) = result
                    incremental_upload(self.session, fname, fd, uploadpath, logger=self.logger)
        finally:
            watcher.clean()

    def _upload_logs_once(self):
        """Upload log updates without waiting for anything"""
        try:
            self._incremental_upload_logs()
        except koji.ActionNotAllowed:
            pass

    def _write_logs(self, build_id, logs_dir, platforms: list = None):
        logfiles = {'noarch': open(os.path.join(logs_dir, self.incremental_log_basename),
                                   'wb')}
        self.logger.info("Will write follow log: %s", self.incremental_log_basename)
        try:
            logs = self.osbs().get_build_logs(build_id, follow=True, wait=True)
        except Exception as error:
            msg = "Exception while waiting for build logs: %s" % error
            raise ContainerError(msg)

        user_warnings = UserWarningsStore()
        final_platforms = []

        for task_run_name, line in logs:
            if METADATA_TAG in line:
                _, meta_file = line.rsplit(' ', 1)
                source_file = os.path.join(koji.pathinfo.work(), meta_file)
                uploadpath = os.path.join(logs_dir, os.path.basename(meta_file))
                shutil.copy(source_file, uploadpath)
                continue

            if user_warnings.is_user_warning(line):
                user_warnings.store(line)
                continue

            if platforms:
                task_platform = next(
                    (platform for platform in platforms if
                     platform.replace('_', '-') in task_run_name),
                    'noarch'
                )
            else:
                task_platform = 'noarch'

            if task_platform not in logfiles:
                if task_platform != 'noarch' and not final_platforms:
                    final_platforms = self.osbs().get_final_platforms(build_id)

                    if not final_platforms:
                        self.logger.info("Couldn't obtain final platforms from build")
                        final_platforms = platforms

                if (task_platform != 'noarch') and (task_platform not in final_platforms):
                    continue

                if task_platform != 'noarch':
                    logfiles['noarch'].write(bytearray(f'{task_platform} build has started. '
                                                       'Check platform specific logs\n', 'utf-8'))

                log_filename = f'{task_platform}.log'
                logfiles[task_platform] = open(os.path.join(logs_dir, log_filename),
                                               'wb')

            outfile = logfiles[task_platform]

            remote_sources_log = None
            if task_run_name == REMOTE_SOURCES_TASKNAME:
                if REMOTE_SOURCES_LOGNAME in logfiles:
                    remote_sources_log = logfiles[REMOTE_SOURCES_LOGNAME]
                else:
                    log_filename = f"{REMOTE_SOURCES_LOGNAME}.log"
                    remote_sources_log = open(os.path.join(logs_dir, log_filename),
                                              'wb')
                    logfiles[REMOTE_SOURCES_LOGNAME] = remote_sources_log

            try:
                outfile.write(("%s\n" % line).encode('utf-8'))
                outfile.flush()
                if remote_sources_log:
                    remote_sources_log.write(("%s\n" % line).encode('utf-8'))
                    remote_sources_log.flush()
            except Exception as error:
                msg = "Exception (%s) while writing build logs: %s" % (type(error), error)
                raise ContainerError(msg)

        for logfile in logfiles.values():
            logfile.close()

        if user_warnings:
            try:
                log_filename = os.path.join(logs_dir, "user_warnings.log")
                with open(log_filename, 'wb') as logfile:
                    logfile.write(str(user_warnings).encode('utf-8'))

                self.logger.info("user_warnings.log written")
            except Exception as error:
                msg = "Exception ({}) while writing user warnings: {}".format(type(error), error)
                raise ContainerError(msg)

        self.logger.info("%s written", self.incremental_log_basename)

    def _write_incremental_logs(self, build_id, logs_dir, platforms: list = None):
        self._write_logs(build_id, logs_dir, platforms=platforms)

        if self.osbs().build_not_finished(build_id):
            raise ContainerError("Build log finished but build still has not "
                                 "finished: %s." % self.osbs().get_build_reason(build_id))

    def _read_user_warnings(self, logs_dir):
        log_filename = os.path.join(logs_dir, "user_warnings.log")

        if os.path.isfile(log_filename):
            try:
                with open(log_filename, 'rb') as logfile:
                    user_warnings = logfile.read().decode('utf-8')
                    return user_warnings.splitlines()
            except Exception as error:
                msg = "Exception ({}) while reading user warnings: {}".format(type(error), error)
                raise ContainerError(msg)

    def check_whitelist(self, name, target_info):
        """Check if container name is whitelisted in destination tag

        Raises with koji.BuildError if package is not whitelisted or blocked.
        """
        pkg_cfg = self.session.getPackageConfig(target_info['dest_tag_name'],
                                                name)
        self.logger.debug("%r", pkg_cfg)
        # Make sure package is on the list for this tag
        if pkg_cfg is None:
            raise koji.BuildError("package (container) %s not in list for tag %s" %
                                  (name, target_info['dest_tag_name']))
        elif pkg_cfg['blocked']:
            raise koji.BuildError("package (container)  %s is blocked for tag %s" %
                                  (name, target_info['dest_tag_name']))

    def handle_build_response(self, build_id, platforms: list = None):
        try:
            return self._handle_build_response(build_id, platforms)
        finally:
            try:
                self.osbs().remove_build(build_id)
            except Exception as error:
                self.logger.warning("Failed to remove build %s : %s", build_id, error)

    def _handle_build_response(self, build_id, platforms: list = None):
        self.logger.debug("OSBS build id: %r", build_id)

        # When builds are cancelled the builder plugin process gets SIGINT and SIGKILL
        # If osbs has started a build it should get cancelled
        def sigint_handler(*args, **kwargs):
            if not build_id:
                return

            self.logger.warning("Cannot read logs, cancelling build %s", build_id)
            self.osbs().cancel_build(build_id)

        signal.signal(signal.SIGINT, sigint_handler)

        osbs_logs_dir = self.resultdir()
        koji.ensuredir(osbs_logs_dir)
        pid = os.fork()
        if pid:
            try:
                self._incremental_upload_logs(pid)
            except koji.ActionNotAllowed:
                pass
        else:
            self._osbs = None

            try:
                self._write_incremental_logs(build_id, osbs_logs_dir, platforms=platforms)
            except Exception as error:
                self.logger.info("Error while saving incremental logs: %s", error)
                os._exit(1)
            os._exit(0)

        # User warnings are being processed in a child process,
        # so we have to collect them back when the process ends
        user_warnings = self._read_user_warnings(osbs_logs_dir)

        # there is race between all pods finished and pipeline run changing status
        self.osbs().wait_for_build_to_finish(build_id)

        has_succeeded = self.osbs().build_has_succeeded(build_id)
        build_results = self.osbs().get_build_results(build_id)

        self.logger.debug("OSBS build finished with status: %s. Build "
                          "response: %s.", self.osbs().get_build_reason(build_id),
                          self.osbs().get_build(build_id))

        self.logger.info("Response status: %r", has_succeeded)

        if self.osbs().build_was_cancelled(build_id):
            self.session.cancelTask(self.id)
            self._upload_logs_once()
            raise ContainerCancelled('Image build was cancelled by OSBS.')

        elif not has_succeeded:
            error_message = None
            try:
                error_message = self.osbs().get_build_error_message(build_id)
            except Exception:
                self.logger.exception("Error during getting error message")

            if self.osbs().build_not_finished(build_id):
                try:
                    self.osbs().cancel_build(build_id)
                except Exception as ex:
                    self.logger.error("Error during canceling pipeline run %s: %s",
                                      build_id, repr(ex))

            self._upload_logs_once()
            if error_message:
                raise ContainerError('Image build failed. %s. OSBS build id: %s' %
                                     (' '.join(error_message.split('\n')), build_id))
            else:
                raise ContainerError('Image build failed. OSBS build id: %s' %
                                     build_id)

        repositories = []
        if has_succeeded:
            repositories = _concat(build_results['repositories'].values())

        self.logger.info("Image available in the following repositories: %r",
                         repositories)

        koji_build_id = None
        if has_succeeded and not self.opts.get('scratch'):
            # Only successful, non-scratch tasks create Koji builds
            # For backward compatibility reasons, koji_build_id has to be a string
            koji_build_id = str(build_results['koji-build-id'])

        self.logger.info("Koji content generator build ID: %s", koji_build_id)

        containerdata = {
            'task_id': self.id,
            'osbs_build_id': build_id,
            'files': [],
            'repositories': repositories,
            'koji_build_id': koji_build_id,
        }

        if user_warnings:
            containerdata['user_warnings'] = user_warnings

        self._upload_logs_once()
        return containerdata


class BuildContainerTask(BaseContainerTask):
    """Start builds via osbs for each arch (this might change soon)"""
    Methods = ['buildContainer']
    # Same value as for regular 'build' method.
    _taskWeight = 2.0

    # JSON Schema definition for koji buildContainer task parameters
    # Used to validate arguments passed to the handler() method of this class
    PARAMS_SCHEMA = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "description": "Parameters for a koji buildContainer task.",

        "type": "array",
        "items": [
            {
                "type": "string",
                "description": "Source URI."
            },
            {
                "type": "string",
                "description": "Build target."
            },
            {
                "type": ["object"],
                "properties": {
                    "scratch": {
                        "type": "boolean",
                        "description": "Perform a scratch build?"
                    },
                    "isolated": {
                        "type": "boolean",
                        "description": "Perform an isolated build?"
                    },
                    "dependency_replacements": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "string"
                        },
                        "description": "Cachito dependency replacements"
                    },
                    "yum_repourls": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "string"
                        },
                        "description": "URLs of yum repo files."
                    },
                    "arch_override": {
                        "type": ["string", "null"],
                        "description": "Limit build to specific arches. "
                                       "Separate each arch with a space."
                    },
                    "git_branch": {
                        "type": ["string", "null"],
                        "description": "Git branch to build from. OSBS uses "
                                       "this to determine which BuildConfig "
                                       "to update."
                    },
                    "koji_parent_build": {
                        "type": ["string", "null"],
                        "description": "Overwrite parent image with image from koji build."
                    },
                    "release": {
                        "type": ["string", "null"],
                        "description": "Set release value."
                    },
                    "flatpak": {
                        "type": "boolean",
                        "description": "Build a flatpak instead of a container?"
                    },
                    "compose_ids": {
                        "type": ["array", "null"],
                        "items": {
                            "type": "integer"
                        },
                        "description": "A list of ODCS composes to use "
                        "during the build. If you do not set this parameter, "
                        "OSBS will request its own ODCS composes based on "
                        "the compose settings in container.yaml. If you set "
                        "this parameter, OSBS will not request its own ODCS "
                        "composes, and it will only use the exact ones you "
                        "specify here."
                    },
                    "signing_intent": {
                        "type": ["string", "null"],
                        "description": "Signing intent of the ODCS composes. "
                        "This must be one of the signing intent names "
                        "configured on the OSBS server, or null. If this "
                        "value is null (the default), the server will use the "
                        "signing intent of the compose_ids you specify, or "
                        "default_signing_intent. To view the full list of "
                        "possible names, see atomic_reactor.config in "
                        "osbs-build.log."
                    },
                    "skip_build": {
                        "type": "boolean",
                        "description": "[DEPRECATED] "
                        "Skip build, just update buildconfig for autorebuild "
                        "and don't start build"
                    },
                    "userdata": {
                        "type": "object",
                        "description": "User defined dictionary containing custom metadata",
                    },
                    "operator_csv_modifications_url": {
                        "type": ["string", "null"],
                        "description": "URL to JSON file with operator CSV modifications",
                    }
                },
                "additionalProperties": False
            }
        ],
        "minItems": 3
    }

    def __init__(self, id, method, params, session, options, workdir=None):
        # pylint: disable=redefined-builtin
        BaseContainerTask.__init__(self, id, method, params, session, options,
                                   workdir)
        self.event_id = None

    def _get_scm(self, src, task_info, scratch):
        scm = My_SCM(src)
        scm_policy_opts = {
            'user_id': task_info['owner'],
            'channel': self.session.getChannel(task_info['channel_id'],
                                               strict=True)['name'],
            'scratch': bool(scratch),
        }
        scm.assert_allowed(
                allowed=self.options.allowed_scms,
                session=self.session,
                by_config=self.options.allowed_scms_use_config,
                by_policy=self.options.allowed_scms_use_policy,
                policy_data=scm_policy_opts)

        return scm

    def createContainer(self, src=None, target_info=None, arches=None,
                        scratch=None, isolated=None, yum_repourls=None,
                        branch=None, koji_parent_build=None, release=None,
                        flatpak=False, signing_intent=None, compose_ids=None,
                        skip_build=None,  # BW compatibility
                        dependency_replacements=None, operator_csv_modifications_url=None,
                        userdata=None):
        if not yum_repourls:
            yum_repourls = []

        if skip_build is not None:
            if skip_build:
                raise koji.BuildError("'skip_build' functionality has been removed")
            else:
                self.logger.warning("deprecated option 'skip_build' in build params")

        this_task = self.session.getTaskInfo(self.id)
        self.logger.debug("This task: %r", this_task)
        owner_info = self.session.getUser(this_task['owner'])
        self.logger.debug("Started by %s", owner_info['name'])

        scm = self._get_scm(src, this_task, scratch)

        git_uri = scm.get_git_uri()
        component = scm.get_component()

        if not arches:
            raise koji.BuildError("arches aren't specified")

        if signing_intent and compose_ids:
            raise koji.BuildError("signing_intent used with compose_ids")

        create_build_args = {
            'git_uri': git_uri,
            'git_ref': scm.revision,
            'user': owner_info['name'],
            'component': component,
            'target': target_info['name'],
            'dependency_replacements': dependency_replacements,
            'yum_repourls': yum_repourls,
            'scratch': scratch,
            'koji_task_id': self.id,
            'platforms': arches,
        }
        if branch:
            create_build_args['git_branch'] = branch
        if flatpak:
            create_build_args['flatpak'] = True
        if operator_csv_modifications_url:
            create_build_args['operator_csv_modifications_url'] = operator_csv_modifications_url
        if userdata:
            create_build_args['userdata'] = userdata
        if signing_intent:
            create_build_args['signing_intent'] = signing_intent
        if compose_ids:
            create_build_args['compose_ids'] = compose_ids
        if koji_parent_build:
            create_build_args['koji_parent_build'] = koji_parent_build
        if isolated:
            create_build_args['isolated'] = isolated
        if release:
            create_build_args['release'] = release

        create_build_args['default_buildtime_limit'] =\
            self.osbs().os_conf.get_default_buildtime_limit()
        create_build_args['max_buildtime_limit'] =\
            self.osbs().os_conf.get_max_buildtime_limit()

        try:
            create_method = self.osbs().create_binary_container_build
            self.logger.debug("Starting %s with params: '%s",
                              create_method, create_build_args)
            build_response = create_method(**create_build_args)
        except AttributeError:
            raise koji.BuildError("method %s doesn't exists in osbs" % create_method)
        except OsbsValidationException as exc:
            raise ContainerError('OSBS validation exception: {0}'.format(exc))

        return self.handle_build_response(self.osbs().get_build_name(build_response),
                                          platforms=arches)

    def getArchList(self, build_tag, extra=None):
        """Copied from build task"""
        # get list of arches to build for
        buildconfig = self.session.getBuildConfig(build_tag, event=self.event_id)
        arches = buildconfig['arches']
        if not arches:
            raise koji.BuildError("No arches for tag %(name)s [%(id)s]" % buildconfig)
        tag_archlist = [koji.canonArch(a) for a in arches.split()]
        self.logger.debug('arches: %s', arches)
        if extra:
            self.logger.debug('Got extra arches: %s', extra)
            arches = "%s %s" % (arches, extra)
        archlist = arches.split()
        self.logger.debug('base archlist: %r', archlist)

        override = self.opts.get('arch_override')
        if (self.opts.get('isolated') or self.opts.get('scratch')) and override:
            # only honor override for scratch builds
            self.logger.debug('arch override: %s', override)
            archlist = override.split()
        elif override:
            raise koji.BuildError("arch-override is only allowed for isolated or scratch builds")
        archdict = {}
        for a in archlist:
            # Filter based on canonical arches for tag
            # This prevents building for an arch that we can't handle
            if a == 'noarch' or koji.canonArch(a) in tag_archlist:
                archdict[a] = 1
        if not archdict:
            raise koji.BuildError("No matching arches were found")
        return list(archdict.keys())

    def fetchDockerfile(self, src, build_tag, scratch):
        """
        Gets Dockerfile. Roughly corresponds to getSRPM method of build task
        """
        this_task = self.session.getTaskInfo(self.id)
        scm = self._get_scm(src, this_task, scratch)

        scmdir = os.path.join(self.workdir, 'sources')

        koji.ensuredir(scmdir)

        logfile = os.path.join(self.workdir, 'checkout-for-labels.log')
        uploadpath = self.getUploadDir()

        koji.ensuredir(uploadpath)

        self.run_callbacks('preSCMCheckout', scminfo=scm.get_info(), build_tag=build_tag,
                           scratch=self.opts.get('scratch', False))

        # Check out sources from the SCM
        sourcedir = scm.checkout(scmdir, self.session, uploadpath, logfile)

        self.run_callbacks("postSCMCheckout", scminfo=scm.get_info(), build_tag=build_tag,
                           scratch=self.opts.get('scratch', False), srcdir=sourcedir)

        fn = os.path.join(sourcedir, 'Dockerfile')
        if not os.path.exists(fn):
            raise koji.BuildError("Dockerfile file missing: %s" % fn)
        return fn

    def checkLabels(self, src, build_tag, scratch, label_overwrites=None):
        label_overwrites = label_overwrites or {}
        dockerfile_path = self.fetchDockerfile(src, build_tag, scratch)
        labels_wrapper = LabelsWrapper(dockerfile_path,
                                       logger_name=self.logger.name,
                                       label_overwrites=label_overwrites)
        missing_labels = labels_wrapper.get_missing_label_ids()
        if missing_labels:
            formatted_labels_list = [labels_wrapper.format_label(label_id) for
                                     label_id in missing_labels]
            msg_template = ("Required LABELs haven't been found in "
                            "Dockerfile: %s.")
            raise koji.BuildError(msg_template %
                                  ', '.join(formatted_labels_list))

        # Make sure the longest tag for the docker image is no more than 128 chars
        # see https://github.com/docker/docker/issues/8445

        data = labels_wrapper.get_data_labels()
        tags = labels_wrapper.get_additional_tags()
        check_nvr = False

        if 'RELEASE' in data and 'VERSION' in data:
            if data['RELEASE'] and data['VERSION']:
                version_release_tag = "%s-%s" % (data['VERSION'], data['RELEASE'])
                tags.append(version_release_tag)
                check_nvr = True

        if tags:
            longest_tag = max(tags, key=len)
            if len(longest_tag) > 128:
                raise koji.BuildError(
                    "Docker cannot create image with a tag longer than 128, "
                    "current version-release tag length is %s" % len(longest_tag))

        if check_nvr:
            return (data['COMPONENT'], labels_wrapper.get_expected_nvr())
        return (data['COMPONENT'], None)

    def handler(self, src, target, opts=None):
        jsonschema.validate([src, target, opts], self.PARAMS_SCHEMA)
        self.opts = opts
        component = None

        if not opts.get('git_branch'):
            raise koji.BuildError("Git branch must be specified")

        if opts.get('scratch') and opts.get('isolated'):
            raise koji.BuildError("Build cannot be both isolated and scratch")

        self.event_id = self.session.getLastEvent()['id']
        target_info = self.session.getBuildTarget(target, event=self.event_id)
        if not target_info:
            raise koji.BuildError("Target `%s` not found" % target)

        build_tag = target_info['build_tag']
        archlist = self.getArchList(build_tag)

        flatpak = opts.get('flatpak', False)
        release_overwrite = opts.get('release')

        if flatpak:
            if not osbs_flatpak_support:
                raise koji.BuildError("osbs-client on koji builder doesn't have Flatpak support")

            expected_nvr = None
        else:
            label_overwrites = {}
            if release_overwrite:
                label_overwrites = {LABEL_NAME_MAP['RELEASE'][0]: release_overwrite}
            component, expected_nvr = self.checkLabels(src, label_overwrites=label_overwrites,
                                                       build_tag=build_tag,
                                                       scratch=opts.get('scratch'))

            # scratch builds do not get imported, and consequently not tagged
            if not self.opts.get('scratch'):
                self.check_whitelist(component, target_info)

        if not SCM.is_scm_url(src):
            raise koji.BuildError('Invalid source specification: %s' % src)

        # Scratch and auto release builds shouldn't be checked for nvr
        if not self.opts.get('scratch') and expected_nvr:
            try:
                build = self.session.getBuild(expected_nvr)
                build_id = build['id']
            except Exception:
                self.logger.info("No build for %s found", expected_nvr, exc_info=True)
            else:
                if build['state'] in (koji.BUILD_STATES['FAILED'], koji.BUILD_STATES['CANCELED']):
                    self.logger.info("Build for %s found, but with reusable state %s",
                                     expected_nvr, build['state'], exc_info=True)
                else:
                    raise koji.BuildError("Build for %s already exists, id %s" %
                                          (expected_nvr, build_id))

        self.logger.debug("Spawning jobs for arches: %r", archlist)

        kwargs = dict(
            src=src,
            target_info=target_info,
            scratch=opts.get('scratch', False),
            isolated=opts.get('isolated', False),
            dependency_replacements=opts.get('dependency_replacements', None),
            yum_repourls=opts.get('yum_repourls', None),
            branch=opts.get('git_branch', None),
            arches=archlist,
            koji_parent_build=opts.get('koji_parent_build'),
            release=release_overwrite,
            flatpak=flatpak,
            signing_intent=opts.get('signing_intent', None),
            compose_ids=opts.get('compose_ids', None),
            operator_csv_modifications_url=opts.get('operator_csv_modifications_url'),
            skip_build=opts.get('skip_build', None),
            userdata=opts.get('userdata', None),
        )

        result = self.createContainer(**kwargs)

        self.logger.debug("Result: %r", result)

        if not result:
            return {
                'repositories': [],
                'koji_builds': [],
                'build': 'skipped',
            }

        return create_task_response(result)


class BuildSourceContainerTask(BaseContainerTask):
    """Start builds via osbs"""
    Methods = ['buildSourceContainer']
    # Same value as for regular 'build' method.
    _taskWeight = 2.0

    # JSON Schema definition for koji buildSourceContainer task parameters
    # Used to validate arguments passed to the handler() method of this class
    PARAMS_SCHEMA = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "description": "Parameters for a koji buildSourceContainer task.",

        "type": "array",
        "items": [
            {
                "type": "string",
                "description": "Build target."
            },
            {
                "type": ["object"],
                "properties": {
                    "scratch": {
                        "type": "boolean",
                        "description": "Perform a scratch build?"
                    },
                    "koji_build_id": {
                        "type": ["integer"],
                        "description": "Koji build id for sources",
                        "examples": [1233, 1234, 1235]
                    },
                    "koji_build_nvr": {
                        "type": ["string"],
                        "description": "Koji build nvr for sources",
                        "examples": [
                           "some_image_build-3.0-30",
                           "another_image_build-4.0-10"
                        ]
                    },
                    "signing_intent": {
                        "type": ["string"],
                        "description": "Signing intent of the ODCS composes. "
                        "This must be one of the signing intent names "
                        "configured on the OSBS server, or null. If this "
                        "value is null (the default), the server will use the "
                        "signing intent of the compose_ids you specify, or "
                        "default_signing_intent. To view the full list of "
                        "possible names, see atomic_reactor.config in "
                        "osbs-build.log."
                    },
                    "userdata": {
                        "type": "object",
                        "description": "User defined dictionary containing custom metadata",
                    },
                },
                "anyOf": [
                    {"required": ["koji_build_nvr"]},
                    {"required": ["koji_build_id"]}
                ],
                "additionalProperties": False
            }
        ],
        "minItems": 2
    }

    def __init__(self, id, method, params, session, options, workdir=None):
        # pylint: disable=redefined-builtin
        BaseContainerTask.__init__(self, id, method, params, session, options, workdir)
        self.event_id = None

    def createSourceContainer(self, target_info=None, scratch=None, component=None,
                              koji_build_id=None, koji_build_nvr=None, signing_intent=None,
                              userdata=None):
        this_task = self.session.getTaskInfo(self.id)
        self.logger.debug("This task: %r", this_task)
        owner_info = self.session.getUser(this_task['owner'])
        self.logger.debug("Started by %s", owner_info['name'])

        create_build_args = {
            'user': owner_info['name'],
            'component': component,
            'sources_for_koji_build_id': koji_build_id,
            'sources_for_koji_build_nvr': koji_build_nvr,
            'target': target_info['name'],
            'scratch': scratch,
            'koji_task_id': self.id,
        }

        if signing_intent:
            create_build_args['signing_intent'] = signing_intent
        if userdata:
            create_build_args['userdata'] = userdata

        try:
            create_method = self.osbs().create_source_container_build
            self.logger.debug("Starting %s with params: '%s",
                              create_method, create_build_args)
            build_response = create_method(**create_build_args)
        except AttributeError:
            raise koji.BuildError("method %s doesn't exists in osbs" % create_method)
        except OsbsValidationException as exc:
            raise ContainerError('OSBS validation exception: {0}'.format(exc))

        return self.handle_build_response(self.osbs().get_build_name(build_response))

    def get_source_build_info(self, build_id, build_nvr):
        build_identifier = build_nvr or build_id

        koji_build = self.session.getBuild(build_identifier)
        if not koji_build:
            raise koji.BuildError("specified source build '%s' doesn't exist" % build_identifier)

        if build_id and (build_id != koji_build['build_id']):
            err_msg = (
                'koji_build_id {} does not match koji_build_nvr {} with id {}. '
                'When specifying both an id and an nvr, they should point to the same image build'
                .format(build_id, build_nvr, koji_build['build_id'])
                )
            raise koji.BuildError(err_msg)

        build_extras = koji_build['extra']
        if 'image' not in build_extras:
            err_msg = ('koji build {} is not image build which source container requires'
                       .format(koji_build['nvr']))
            raise koji.BuildError(err_msg)

        elif 'sources_for_nvr' in koji_build['extra']['image']:
            err_msg = ('koji build {} is source container build, source container can not '
                       'use source container build image'.format(koji_build['nvr']))
            raise koji.BuildError(err_msg)

        if not build_id:
            build_id = koji_build['build_id']
        if not build_nvr:
            build_nvr = koji_build['nvr']
        component = "%s-source" % koji_build['name']

        return component, build_id, build_nvr

    def handler(self, target, opts=None):
        jsonschema.validate([target, opts], self.PARAMS_SCHEMA)
        self.opts = opts

        self.event_id = self.session.getLastEvent()['id']
        target_info = self.session.getBuildTarget(target, event=self.event_id)
        if not target_info:
            raise koji.BuildError("Target `%s` not found" % target)

        component, build_id, build_nvr = self.get_source_build_info(opts.get('koji_build_id'),
                                                                    opts.get('koji_build_nvr'))
        # scratch builds do not get imported, and consequently not tagged
        if not self.opts.get('scratch'):
            self.check_whitelist(component, target_info)

        self.logger.debug("Spawning job for sources")

        kwargs = dict(
            target_info=target_info,
            scratch=opts.get('scratch', False),
            component=component,
            koji_build_id=build_id,
            koji_build_nvr=build_nvr,
            signing_intent=opts.get('signing_intent', None),
            userdata=opts.get('userdata', None),
        )

        result = self.createSourceContainer(**kwargs)

        self.logger.debug("Result: %r", result)

        return create_task_response(result)
