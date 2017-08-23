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
import os
import os.path
import sys
import logging
import imp
import time
import traceback
import dockerfile_parse
import pycurl
import signal

import koji
from koji.daemon import SCM
from koji.tasks import ServerExit, BaseTaskHandler

from osbs.api import OSBS
from osbs.conf import Configuration
from osbs.exceptions import OsbsValidationException

# We need kojid module which isn't proper python module and not even in
# site-package path.
kojid_exe_path = '/usr/sbin/kojid'
try:
    with file(kojid_exe_path, 'U') as fo:
        kojid = imp.load_module('kojid', fo, fo.name, ('.py', 'U', 1))
except IOError:
    kojid = None


# List of LABEL identifiers used within Koji. Values doesn't need to correspond
# to actual LABEL names. All these are required unless there exist default
# value (see LABEL_DEFAULT_VALUES).
LABELS = koji.Enum((
    'COMPONENT',
    'VERSION',
    'RELEASE',
    'ARCHITECTURE',
))


# Mapping between LABEL identifiers within koji and actual LABEL identifiers
# which can be found in Dockerfile. First value is preferred one, others are for
# compatibility purposes.
LABEL_NAME_MAP = {
    'COMPONENT': ('com.redhat.component', 'BZComponent'),
    'VERSION': ('version', 'Version'),
    'RELEASE': ('release', 'Release'),
    'ARCHITECTURE': ('architecture', 'Architecture'),
}


# Map from LABELS to extra data
LABEL_DATA_MAP = {
    'COMPONENT': 'name',
    'VERSION': 'version',
    'RELEASE': 'release',
    'ARCHITECTURE': 'architecture',
}


# Default values for LABELs. If there exist default value here LABEL is
# optional in Dockerfile.
LABEL_DEFAULT_VALUES = {
    'RELEASE': object(), # Symbol-like marker to indicate unique init value
    'ARCHITECTURE': 'x86_64',
}


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
            if fname.endswith('.log') and fname not in self._logs:
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
                fd = file(fpath, 'r')
            self._logs[fname] = (fd, stat_info.st_ino, stat_info.st_size, fpath)
        except OSError:
            self.logger.error("The build has been cancelled")
            raise koji.ActionNotAllowed
        except:
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

        returns dict with keys from LABELS and default values from
        LABEL_DEFAULT_VALUES or actual values from Dockefile as mapped via
        LABEL_NAME_MAP.
        """

        parsed_labels = self.get_labels()
        for label_id in LABELS:
            assert label_id in LABEL_NAME_MAP, ("Required LABEL doesn't map "
                                                "to LABEL name in Dockerfile")
            self._label_data.setdefault(label_id,
                                        LABEL_DEFAULT_VALUES.get(label_id,
                                                                 None))
            for label_name in LABEL_NAME_MAP[label_id]:
                if label_name in self._label_overwrites:
                    self._label_data[label_id] = self._label_overwrites[label_name]
                    break

                if label_name in parsed_labels:
                    self._label_data[label_id] = parsed_labels[label_name]
                    break
        return self._label_data

    def get_extra_data(self):
        """Returns dict with keys for Koji's extra_information"""
        data = self.get_data_labels()
        extra_data = {}
        for label_id, value in data.items():
            assert label_id in LABEL_DATA_MAP
            extra_key = LABEL_DATA_MAP[label_id]
            extra_data[extra_key] = value
        return extra_data

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
        finally:
            return tags

    def get_missing_label_ids(self):
        data = self.get_data_labels()
        missing_labels = []
        for label_id in LABELS:
            assert label_id in data
            if not data[label_id]:
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


class BuildContainerTask(BaseTaskHandler):
    # Start builds via osbs for each arch (this might change soon)
    Methods = ['buildContainer']
    # Same value as for regular 'build' method.
    _taskWeight = 2.0

    def __init__(self, id, method, params, session, options, workdir=None):
        BaseTaskHandler.__init__(self, id, method, params, session, options,
                                 workdir)
        self._osbs = None

        # Check that the kojid module was successfully imported
        assert kojid

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
        return self._osbs

    def getUploadPath(self):
        """Get the path that should be used when uploading files to
        the hub."""
        return koji.pathinfo.taskrelpath(self.id)

    def resultdir(self):
        return os.path.join(self.workdir, 'osbslogs')

    def _incremental_upload_logs(self, child_pid):
        resultdir = self.resultdir()
        uploadpath = self.getUploadPath()
        watcher = FileWatcher(resultdir, logger=self.logger)
        finished = False
        try:
            while not finished:
                time.sleep(1)
                status = os.waitpid(child_pid, os.WNOHANG)
                if status[0] != 0:
                    finished = True

                for result in watcher.files_to_upload():
                    if result is False:
                        return
                    (fd, fname) = result
                    kojid.incremental_upload(self.session, fname, fd,
                                             uploadpath, logger=self.logger)
        finally:
            watcher.clean()

    def _write_incremental_logs(self, build_id, log_filename):
        log_basename = os.path.basename(log_filename)
        self.logger.info("Will write follow log: %s", log_basename)
        try:
            build_logs = self.osbs().get_build_logs(build_id,
                                                    follow=True)
        except Exception, error:
            msg = "Exception while waiting for build logs: %s" % error
            raise ContainerError(msg)
        outfile = open(log_filename, 'w')
        try:
            for line in build_logs:
                outfile.write("%s\n" % line)
                outfile.flush()
        except Exception, error:
            msg = "Exception (%s) while reading build logs: %s" % (type(error),
                                                                   error)
            raise ContainerError(msg)
        finally:
            outfile.close()
        self.logger.info("%s written", log_basename)
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
        except Exception, error:
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
        self.logger.debug("%r" % pkg_cfg)
        # Make sure package is on the list for this tag
        if pkg_cfg is None:
            raise koji.BuildError("package (container) %s not in list for tag %s" % (name, target_info['dest_tag_name']))
        elif pkg_cfg['blocked']:
            raise koji.BuildError("package (container)  %s is blocked for tag %s" % (name, target_info['dest_tag_name']))

    def runBuilds(self, src, target_info, arches, scratch=False, isolated=False,
                  yum_repourls=None, branch=None, push_url=None,
                  koji_parent_build=None, release=None):

        self.logger.debug("Spawning jobs for arches: %r" % (arches))

        results = []

        kwargs = dict(
            src=src,
            target_info=target_info,
            scratch=scratch,
            isolated=isolated,
            yum_repourls=yum_repourls,
            branch=branch,
            push_url=push_url,
            arches=arches,
            koji_parent_build=koji_parent_build,
            release=release,
        )

        results = [self.createContainer(**kwargs)]

        self.logger.debug("Results: %r", results)
        return results

    def createContainer(self, src=None, target_info=None, arches=None,
                        scratch=None, isolated=None, yum_repourls=[],
                        branch=None, push_url=None, koji_parent_build=None,
                        release=None):
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
            raise ContainerError("arches aren't specified")

        create_build_args = {
            'git_uri': git_uri,
            'git_ref': scm.revision,
            'user': owner_info['name'],
            'component': component,
            'target': target_info['name'],
            'yum_repourls': yum_repourls,
            'scratch': scratch,
            'koji_task_id': self.id,
            'architecture': arch
        }
        if branch:
            create_build_args['git_branch'] = branch
        if push_url:
            create_build_args['git_push_url'] = push_url

        try:
            orchestrator_create_build_args = create_build_args.copy()
            orchestrator_create_build_args['platforms'] = arches
            if koji_parent_build:
                orchestrator_create_build_args['koji_parent_build'] = koji_parent_build
            if isolated:
                orchestrator_create_build_args['isolated'] = isolated
            if release:
                orchestrator_create_build_args['release'] = release

            create_method = self.osbs().create_orchestrator_build
            self.logger.debug("Starting %s with params: '%s",
                              create_method, orchestrator_create_build_args)
            build_response = create_method(**orchestrator_create_build_args)
        except (AttributeError, OsbsValidationException):
            # Older osbs-client, or else orchestration not enabled
            create_build_args['architecture'] = arch = arches[0]
            create_method = self.osbs().create_build
            self.logger.debug("Starting %s with params: '%s'",
                              create_method, create_build_args)
            build_response = create_method(**create_build_args)

        build_id = build_response.get_build_name()
        self.logger.debug("OSBS build id: %r", build_id)

        # When builds are cancelled the builder plugin process gets SIGINT and SIGKILL
        # If osbs has started a build it should get cancelled
        def sigint_handler(*args, **kwargs):
            if not build_id:
                return

            self.logger.warn("Cannot read logs, cancelling build %s", build_id)
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
            full_output_name = os.path.join(osbs_logs_dir,
                                            'openshift-incremental.log')

            # Make sure curl is initialized again otherwise connections via SSL
            # fails with NSS error -8023 and curl_multi.info_read()
            # returns error code 35 (SSL CONNECT failed).
            # See http://permalink.gmane.org/gmane.comp.web.curl.library/38759
            self._osbs = None
            self.logger.debug("Running pycurl global cleanup")
            pycurl.global_cleanup()

            # Following retry code is here mainly to workaround bug which causes
            # connection drop while reading logs after about 5 minutes.
            # OpenShift bug with description:
            # https://github.com/openshift/origin/issues/2348
            # and upstream bug in Kubernetes:
            # https://github.com/GoogleCloudPlatform/kubernetes/issues/9013
            retry = 0
            max_retries = 30
            while retry < max_retries:
                try:
                    self._write_incremental_logs(build_id,
                                                 full_output_name)
                except Exception, error:
                    self.logger.info("Error while saving incremental logs "
                                     "(retry #%d): %s", retry, error)
                    retry += 1
                    time.sleep(10)
                    continue
                break
            else:
                self.logger.info("Gave up trying to save incremental logs "
                                 "after #%d retries.", retry)
                os._exit(1)
            os._exit(0)

        response = self.osbs().wait_for_build_to_finish(build_id)

        self.logger.debug("OSBS build finished with status: %s. Build "
                          "response: %s.", response.status,
                          response.json)

        self.logger.info("Response status: %r", response.is_succeeded())

        if response.is_cancelled():
            self.session.cancelTask(self.id)
            raise ContainerCancelled(
                'Image build was cancelled by OSBS, maybe by automated rebuild.'
            )

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
            'arch': arch,
            'task_id': self.id,
            'osbs_build_id': build_id,
            'files': [],
            'repositories': repositories,
            'koji_build_id': koji_build_id,
        }

        return containerdata

    def getArchList(self, build_tag, extra=None):
        """Copied from build task"""
        # get list of arches to build for
        buildconfig = self.session.getBuildConfig(build_tag, event=self.event_id)
        arches = buildconfig['arches']
        if not arches:
            # XXX - need to handle this better
            raise koji.BuildError, "No arches for tag %(name)s [%(id)s]" % buildconfig
        tag_archlist = [koji.canonArch(a) for a in arches.split()]
        self.logger.debug('arches: %s', arches)
        if extra:
            self.logger.debug('Got extra arches: %s', extra)
            arches = "%s %s" % (arches, extra)
        archlist = arches.split()
        self.logger.debug('base archlist: %r' % archlist)

        override = self.opts.get('arch_override')
        if self.opts.get('scratch') and override:
            # only honor override for scratch builds
            self.logger.debug('arch override: %s', override)
            archlist = override.split()
        archdict = {}
        for a in archlist:
            # Filter based on canonical arches for tag
            # This prevents building for an arch that we can't handle
            if a == 'noarch' or koji.canonArch(a) in tag_archlist:
                archdict[a] = 1
        if not archdict:
            raise koji.BuildError("No matching arches were found")
        return archdict.keys()

    def fetchDockerfile(self, src):
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

        # Check out sources from the SCM
        sourcedir = scm.checkout(scmdir, self.session, uploadpath, logfile)

        fn = os.path.join(sourcedir, 'Dockerfile')
        if not os.path.exists(fn):
            raise koji.BuildError, "Dockerfile file missing: %s" % fn
        return fn

    def _get_admin_opts(self, opts):
        epoch = opts.get('epoch', 0)
        if epoch:
            self.session.assertPerm('admin')

        return {'epoch': epoch}

    def checkLabels(self, src, label_overwrites=None):
        label_overwrites = label_overwrites or {}
        dockerfile_path = self.fetchDockerfile(src)
        labels_wrapper = LabelsWrapper(dockerfile_path,
                                       logger_name=self.logger.name,
                                       label_overwrites=label_overwrites)
        missing_labels = labels_wrapper.get_missing_label_ids()
        if missing_labels:
            formatted_labels_list = [labels_wrapper.format_label(label_id) for
                                     label_id in missing_labels]
            msg_template = ("Required LABELs haven't been found in "
                            "Dockerfile: %s.")
            raise koji.BuildError, (msg_template %
                                    ', '.join(formatted_labels_list))

        # Make sure the longest tag for the docker image is no more than 128 chars
        # see https://github.com/docker/docker/issues/8445

        data = labels_wrapper.get_extra_data()
        tags = labels_wrapper.get_additional_tags()
        if LABEL_DATA_MAP['RELEASE'] in data:
            version_release_tag = "%s-%s" % (
                data[LABEL_DATA_MAP['VERSION']], data[LABEL_DATA_MAP['RELEASE']])
            tags.append(version_release_tag)
        if tags:
            longest_tag = max(tags, key=len)
            if len(longest_tag) > 128:
                raise koji.BuildError(
                    "Docker cannot create image with a tag longer than 128, "
                    "current version-release tag length is %s" % len(longest_tag))

        return (labels_wrapper.get_extra_data(), labels_wrapper.get_expected_nvr())

    def handler(self, src, target, opts=None):
        if not opts:
            opts = {}
        self.opts = opts
        data = {}

        self.event_id = self.session.getLastEvent()['id']
        target_info = self.session.getBuildTarget(target, event=self.event_id)
        build_tag = target_info['build_tag']
        archlist = self.getArchList(build_tag)

        label_overwrites = {}
        release_overwrite = opts.get('release')
        if release_overwrite:
            label_overwrites = {LABEL_DATA_MAP['RELEASE']: release_overwrite}
        data, expected_nvr = self.checkLabels(src, label_overwrites=label_overwrites)

        admin_opts = self._get_admin_opts(opts)
        data.update(admin_opts)

        # scratch builds do not get imported, and consequently not tagged
        if not self.opts.get('scratch'):
            self.check_whitelist(data[LABEL_DATA_MAP['COMPONENT']], target_info)

        try:
            auto_release = (data[LABEL_DATA_MAP['RELEASE']] ==
                            LABEL_DEFAULT_VALUES['RELEASE'])
            if auto_release:
                # Do not expose default release value
                del data[LABEL_DATA_MAP['RELEASE']]

            self.extra_information = {"src": src, "data": data,
                                      "target": target}

            if not SCM.is_scm_url(src):
                raise koji.BuildError('Invalid source specification: %s' % src)

            # Scratch and auto release builds shouldn't be checked for nvr
            if not self.opts.get('scratch') and not auto_release:
                try:
                    build_id = self.session.getBuild(expected_nvr)['id']
                except:
                    self.logger.info("No build for %s found", expected_nvr, exc_info=True)
                else:
                    raise koji.BuildError(
                        "Build for %s already exists, id %s" % (expected_nvr, build_id))

            results = self.runBuilds(src, target_info, archlist,
                                     scratch=opts.get('scratch', False),
                                     isolated=opts.get('isolated', False),
                                     yum_repourls=opts.get('yum_repourls', None),
                                     branch=opts.get('git_branch', None),
                                     push_url=opts.get('push_url', None),
                                     koji_parent_build=opts.get('koji_parent_build'),
                                     release=release_overwrite,
                                     )
            all_repositories = []
            all_koji_builds = []
            for result in results:
                try:
                    repository = result.get('repositories')
                    all_repositories.extend(repository)
                except Exception, error:
                    self.logger.error("Failed to merge list of repositories "
                                      "%r. Reason (%s): %s", repository,
                                      type(error), error)
                koji_build_id = result.get('koji_build_id')
                if koji_build_id:
                    all_koji_builds.append(koji_build_id)

        except (SystemExit, ServerExit, KeyboardInterrupt):
            # we do not trap these
            raise
        except:
            # reraise the exception
            raise

        return {
            'repositories': all_repositories,
            'koji_builds': all_koji_builds,
        }
