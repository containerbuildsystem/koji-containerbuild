"""Koji builder plugin extends Koji to build containers

Acts as a wrapper between Koji and OpenShift builsystem via osbs for building
containers."""

# Copyright (C) 2015  Red Hat, Inc.
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
import json
import jsonschema
from six import StringIO

# this is present because in some versions of koji, callback functions assume koji.plugin is
# imported
import koji.plugin
import koji
from koji.daemon import SCM, incremental_upload
from koji.tasks import BaseTaskHandler

import osbs
from osbs.api import OSBS
from osbs.conf import Configuration
try:
    from osbs.exceptions import OsbsOrchestratorNotEnabled
except ImportError:
    from osbs.exceptions import OsbsValidationException as OsbsOrchestratorNotEnabled

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


METADATA_TAG = "_metadata_"

ANNOTATIONS_FILENAME = 'build_annotations.json'

class ContainerError(koji.GenericError):
    """Raised when container creation fails"""
    faultCode = 2001

class ContainerCancelled(koji.GenericError):
    """Raised when container creation is cancelled by OSBS"""
    faultCode = 2002


# TODO: push this to upstream koji
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
            assert label_id in LABEL_NAME_MAP, ("Required LABEL doesn't map "
                                                "to LABEL name in Dockerfile")

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

        assert label_id in LABEL_NAME_MAP
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
        self.demux = None
        self._log_handler_added = False
        self.incremental_log_basename = 'openshift-incremental.log'

    def osbs(self):
        """Handler of OSBS object"""
        if not self._osbs:
            os_conf = Configuration()
            build_conf = Configuration()
            if self.opts.get('scratch'):
                os_conf = Configuration(conf_section='scratch')
                build_conf = Configuration(conf_section='scratch')
            self._osbs = OSBS(os_conf, build_conf)
            assert self._osbs
            self.setup_osbs_logging()

        return self._osbs

    def setup_osbs_logging(self):
        # Setting handler more than once will cause duplicated log lines.
        # Log handler will persist in child process.
        if not self._log_handler_added:
            osbs_logger = logging.getLogger(osbs.__name__)
            osbs_logger.setLevel(logging.INFO)
            log_file = os.path.join(self.resultdir(), 'osbs-client.log')
            handler = logging.FileHandler(filename=log_file)
            # process (PID) is useful because a buildContainer task forks main process
            formatter = logging.Formatter(
                '%(asctime)s - %(process)d - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            osbs_logger.addHandler(handler)

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

    def _write_combined_log(self, build_id, logs_dir):
        log_filename = os.path.join(logs_dir, self.incremental_log_basename)

        self.logger.info("Will write follow log: %s", self.incremental_log_basename)
        try:
            log = self.osbs().get_build_logs(build_id, follow=True)
        except Exception as error:
            msg = "Exception while waiting for build logs: %s" % error
            raise ContainerError(msg)
        with open(log_filename, 'wb') as outfile:
            try:
                for line in log:
                    outfile.write(("%s\n" % line).encode('utf-8'))
                    outfile.flush()
            except Exception as error:
                msg = "Exception (%s) while writing build logs: %s" % (type(error),
                                                                       error)
                raise ContainerError(msg)

        self.logger.info("%s written", self.incremental_log_basename)

    def _write_demultiplexed_logs(self, build_id, logs_dir):
        self.logger.info("Will write demuxed logs in: %s/", logs_dir)
        try:
            logs = self.osbs().get_orchestrator_build_logs(build_id, follow=True)
        except Exception as error:
            msg = "Exception while waiting for orchestrator build logs: %s" % error
            raise ContainerError(msg)
        platform_logs = {}
        for entry in logs:
            platform = entry.platform
            if platform == METADATA_TAG:
                meta_file = entry.line
                source_file = os.path.join(koji.pathinfo.work(), meta_file)
                uploadpath = os.path.join(logs_dir, os.path.basename(meta_file))
                shutil.copy(source_file, uploadpath)
                continue

            if platform not in platform_logs:
                prefix = 'orchestrator' if platform is None else platform
                log_filename = os.path.join(logs_dir, "%s.log" % prefix)
                platform_logs[platform] = open(log_filename, 'wb')
            try:
                platform_logs[platform].write((entry.line + '\n').encode('utf-8'))
                platform_logs[platform].flush()
            except Exception as error:
                msg = "Exception ({}) while writing build logs: {}".format(type(error), error)
                raise ContainerError(msg)
        for logfile in platform_logs.values():
            logfile.close()
            self.logger.info("%s written", logfile.name)

    def _write_incremental_logs(self, build_id, logs_dir):
        if self.demux and hasattr(self.osbs(), 'get_orchestrator_build_logs'):
            self._write_demultiplexed_logs(build_id, logs_dir)
        else:
            self._write_combined_log(build_id, logs_dir)

        build_response = self.osbs().get_build(build_id)
        if (build_response.is_running() or build_response.is_pending()):
            raise ContainerError("Build log finished but build still has not "
                                 "finished: %s." % build_response.status)

    def _get_repositories(self, response):
        repositories = []
        try:
            repo_dict = response.get_repositories()
            if repo_dict:
                for repos in repo_dict.values():
                    repositories.extend(repos)
        except Exception as error:
            self.logger.error("Failed to get available repositories from: %r. "
                              "Reason(%s): %s",
                              repo_dict, type(error), error)
        return repositories

    def _get_koji_build_id(self, response):
        koji_build_id = None
        if hasattr(response, "get_koji_build_id"):
            koji_build_id = response.get_koji_build_id()
        else:
            self.logger.info("Koji content generator build ID not available.")

        return koji_build_id

    def _get_error_message(self, response):
        error_message = None
        if hasattr(response, "get_error_message"):
            error_message = response.get_error_message()
        else:
            self.logger.info("Error message is not available")

        return error_message

    def check_whitelist(self, name, target_info):
        """Check if container name is whitelisted in destination tag

        Raises with koji.BuildError if package is not whitelisted or blocked.
        """
        pkg_cfg = self.session.getPackageConfig(target_info['dest_tag_name'],
                                                name)
        self.logger.debug("%r", pkg_cfg)
        # Make sure package is on the list for this tag
        if pkg_cfg is None:
            raise koji.BuildError("package (container) %s not in list for tag %s" % (name, target_info['dest_tag_name']))
        elif pkg_cfg['blocked']:
            raise koji.BuildError("package (container)  %s is blocked for tag %s" % (name, target_info['dest_tag_name']))

    def upload_build_annotations(self, build_response):
        annotations = build_response.get_annotations() or {}
        whitelist_str = annotations.get('koji_task_annotations_whitelist', "[]")
        whitelist = json.loads(whitelist_str)
        task_annotations = {k: v for k, v in annotations.items() if k in whitelist}
        if task_annotations:
            f = StringIO()
            json.dump(task_annotations, f, sort_keys=True, indent=4)
            f.seek(0)
            incremental_upload(self.session, ANNOTATIONS_FILENAME, f, self.getUploadPath(),
                               logger=self.logger)

    def handle_build_response(self, build_response, arch=None):
        build_id = build_response.get_build_name()
        self.logger.debug("OSBS build id: %r", build_id)

        # When builds are cancelled the builder plugin process gets SIGINT and SIGKILL
        # If osbs has started a build it should get cancelled
        def sigint_handler(*args, **kwargs):
            if not build_id:
                return

            self.logger.warning("Cannot read logs, cancelling build %s", build_id)
            self.osbs().cancel_build(build_id)

        signal.signal(signal.SIGINT, sigint_handler)

        self.logger.debug("Waiting for osbs build_id: %s to be scheduled.",
                          build_id)
        # we need to wait for kubelet to schedule the build, otherwise it's 500
        self.osbs().wait_for_build_to_get_scheduled(build_id)
        self.logger.debug("Build was scheduled")

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
                self._write_incremental_logs(build_id, osbs_logs_dir)
            except Exception as error:
                self.logger.info("Error while saving incremental logs: %s", error)
                os._exit(1)
            os._exit(0)

        response = self.osbs().wait_for_build_to_finish(build_id)
        if response.is_succeeded():
            self.upload_build_annotations(response)

        self.logger.debug("OSBS build finished with status: %s. Build "
                          "response: %s.", response.status,
                          response.json)

        self.logger.info("Response status: %r", response.is_succeeded())

        if response.is_cancelled():
            self.session.cancelTask(self.id)
            raise ContainerCancelled('Image build was cancelled by OSBS.')

        elif response.is_failed():
            error_message = self._get_error_message(response)
            if error_message:
                raise ContainerError('Image build failed. %s. OSBS build id: %s' %
                                     (error_message, build_id))
            else:
                raise ContainerError('Image build failed. OSBS build id: %s' %
                                     build_id)

        repositories = []
        if response.is_succeeded():
            repositories = self._get_repositories(response)

        self.logger.info("Image available in the following repositories: %r",
                         repositories)

        koji_build_id = None
        if response.is_succeeded():
            koji_build_id = self._get_koji_build_id(response)

        self.logger.info("Koji content generator build ID: %s", koji_build_id)

        containerdata = {
            'task_id': self.id,
            'osbs_build_id': build_id,
            'files': [],
            'repositories': repositories,
            'koji_build_id': koji_build_id,
        }
        if arch:
            containerdata['arch'] = arch

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
                    "push_url": {
                        "type": ["string", "null"]
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
                        "description": "ODCS composes used."
                    },
                    "signing_intent": {
                        "type": ["string", "null"],
                        "description": "Signing intent of the ODCS composes."
                    },
                    "skip_build": {
                        "type": "boolean",
                        "description": "Skip build, just update buildconfig for autorebuild "
                                       "and don't start build"
                    },
                    "triggered_after_koji_task": {
                        "type": "integer",
                        "description": "Koji task for which autorebuild runs"
                    },
                    "userdata": {
                        "type": "object",
                        "description": "User defined dictionary containing custom metadata",
                    },
                },
                "additionalProperties": False
            }
        ],
        "minItems": 3
    }

    def __init__(self, id, method, params, session, options, workdir=None, demux=True):
        # pylint: disable=redefined-builtin
        BaseContainerTask.__init__(self, id, method, params, session, options,
                                   workdir)
        self.demux = demux
        self.event_id = None

    def createContainer(self, src=None, target_info=None, arches=None,
                        scratch=None, isolated=None, yum_repourls=None,
                        branch=None, push_url=None, koji_parent_build=None,
                        release=None, flatpak=False, signing_intent=None,
                        compose_ids=None, skip_build=False, triggered_after_koji_task=None,
                        dependency_replacements=None):
        if not yum_repourls:
            yum_repourls = []

        this_task = self.session.getTaskInfo(self.id)
        self.logger.debug("This task: %r", this_task)
        owner_info = self.session.getUser(this_task['owner'])
        self.logger.debug("Started by %s", owner_info['name'])

        scm = My_SCM(src)
        scm.assert_allowed(self.options.allowed_scms)
        git_uri = scm.get_git_uri()
        component = scm.get_component()
        arch = None

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
            'architecture': arch,
        }
        if branch:
            create_build_args['git_branch'] = branch
        if push_url:
            create_build_args['git_push_url'] = push_url
        if flatpak:
            create_build_args['flatpak'] = True
        if skip_build:
            create_build_args['skip_build'] = True
        if triggered_after_koji_task is not None:
            create_build_args['triggered_after_koji_task'] = triggered_after_koji_task

        orchestrator_create_build_args = create_build_args.copy()
        orchestrator_create_build_args['platforms'] = arches
        if signing_intent:
            orchestrator_create_build_args['signing_intent'] = signing_intent
        if compose_ids:
            orchestrator_create_build_args['compose_ids'] = compose_ids
        if koji_parent_build:
            orchestrator_create_build_args['koji_parent_build'] = koji_parent_build
        if isolated:
            orchestrator_create_build_args['isolated'] = isolated
        if release:
            orchestrator_create_build_args['release'] = release

        try:
            create_method = self.osbs().create_orchestrator_build
            self.logger.debug("Starting %s with params: '%s",
                              create_method, orchestrator_create_build_args)
            build_response = create_method(**orchestrator_create_build_args)
        except (AttributeError, OsbsOrchestratorNotEnabled):
            # Older osbs-client, or else orchestration not enabled
            create_build_args['architecture'] = arch = arches[0]
            create_build_args.pop('skip_build', None)
            create_method = self.osbs().create_build
            self.logger.debug("Starting %s with params: '%s'",
                              create_method, create_build_args)
            build_response = create_method(**create_build_args)

        if build_response is None:
            self.logger.debug("Build was skipped")

            osbs_logs_dir = self.resultdir()
            koji.ensuredir(osbs_logs_dir)
            try:
                self._incremental_upload_logs()
            except koji.ActionNotAllowed:
                pass

            return

        return self.handle_build_response(build_response, arch=arch)

    def getArchList(self, build_tag, extra=None):
        """Copied from build task"""
        # get list of arches to build for
        buildconfig = self.session.getBuildConfig(build_tag, event=self.event_id)
        arches = buildconfig['arches']
        if not arches:
            # XXX - need to handle this better
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

    def fetchDockerfile(self, src, build_tag):
        """
        Gets Dockerfile. Roughly corresponds to getSRPM method of build task
        """
        scm = SCM(src)
        scm.assert_allowed(self.options.allowed_scms)
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

    def checkLabels(self, src, build_tag, label_overwrites=None):
        label_overwrites = label_overwrites or {}
        dockerfile_path = self.fetchDockerfile(src, build_tag)
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
        if flatpak:
            if not osbs_flatpak_support:
                raise koji.BuildError("osbs-client on koji builder doesn't have Flatpak support")
            release_overwrite = None
        else:
            label_overwrites = {}
            release_overwrite = opts.get('release')
            if release_overwrite:
                label_overwrites = {LABEL_NAME_MAP['RELEASE'][0]: release_overwrite}
            component, expected_nvr = self.checkLabels(src, label_overwrites=label_overwrites,
                                                       build_tag=build_tag)

        # scratch builds do not get imported, and consequently not tagged
        if not self.opts.get('scratch') and not flatpak:
            self.check_whitelist(component, target_info)

        if flatpak:
            expected_nvr = None

        if not SCM.is_scm_url(src):
            raise koji.BuildError('Invalid source specification: %s' % src)

        # don't check build nvr for autorebuild (has triggered_after_koji_task)
        # as they might be using add_timestamp_to_release
        # and don't check it for skipped build, which might be enabling/disabling
        # autorebuilds which use add_timestamp_to_release
        triggered_after_koji_task = opts.get('triggered_after_koji_task', None)
        skip_build = opts.get('skip_build', False)
        if triggered_after_koji_task or skip_build:
            expected_nvr = None

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
            push_url=opts.get('push_url', None),
            arches=archlist,
            koji_parent_build=opts.get('koji_parent_build'),
            release=release_overwrite,
            flatpak=flatpak,
            signing_intent=opts.get('signing_intent', None),
            compose_ids=opts.get('compose_ids', None),
            skip_build=skip_build,
            triggered_after_koji_task=triggered_after_koji_task,
        )

        results = []
        semi_results = self.createContainer(**kwargs)
        if semi_results is not None:
            results = [semi_results]

        self.logger.debug("Results: %r", results)

        all_repositories = []
        all_koji_builds = []

        if not results:
            return {
                'repositories': all_repositories,
                'koji_builds': all_koji_builds,
                'build': 'skipped',
            }

        for result in results:
            try:
                repository = result.get('repositories')
                all_repositories.extend(repository)
            except Exception as error:
                self.logger.error("Failed to merge list of repositories "
                                  "%r. Reason (%s): %s", repository,
                                  type(error), error)
            koji_build_id = result.get('koji_build_id')
            if koji_build_id:
                all_koji_builds.append(koji_build_id)

        return {
            'repositories': all_repositories,
            'koji_builds': all_koji_builds,
        }


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
                        "description": "Signing intent of the ODCS composes."
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

    def __init__(self, id, method, params, session, options, workdir=None, demux=False):
        # pylint: disable=redefined-builtin
        BaseContainerTask.__init__(self, id, method, params, session, options, workdir)
        self.demux = demux
        self.event_id = None
        self.incremental_log_basename = 'orchestrator.log'

    def createSourceContainer(self, target_info=None, scratch=None, component=None,
                              koji_build_id=None, koji_build_nvr=None, signing_intent=None):
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

        try:
            create_method = self.osbs().create_source_container_build
            self.logger.debug("Starting %s with params: '%s",
                              create_method, create_build_args)
            build_response = create_method(**create_build_args)
        except AttributeError:
            raise koji.BuildError("method %s doesn't exists in osbs" % create_method)

        return self.handle_build_response(build_response)

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
        )

        results = []
        semi_results = self.createSourceContainer(**kwargs)
        if semi_results is not None:
            results = [semi_results]

        self.logger.debug("Results: %r", results)

        all_repositories = []
        all_koji_builds = []

        for result in results:
            try:
                repository = result.get('repositories')
                all_repositories.extend(repository)
            except Exception as error:
                self.logger.error("Failed to merge list of repositories "
                                  "%r. Reason (%s): %s", repository,
                                  type(error), error)
            koji_build_id = result.get('koji_build_id')
            if koji_build_id:
                all_koji_builds.append(koji_build_id)

        return {
            'repositories': all_repositories,
            'koji_builds': all_koji_builds,
        }
