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
import urlgrabber
import urlgrabber.grabber
import dockerfile_parse
import pycurl

import koji
from koji.daemon import SCM
from koji.tasks import ServerExit, BaseTaskHandler

import osbs
import osbs.core
import osbs.api
import osbs.http
from osbs.api import OSBS
from osbs.conf import Configuration

# We need kojid module which isn't proper python module and not even in
# site-package path.
kojid_exe_path = '/usr/sbin/kojid'
fo = file(kojid_exe_path, 'U')
try:
    kojid = imp.load_module('kojid', fo, fo.name, ('.py', 'U', 1))
finally:
    fo.close()


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
    def __init__(self, dockerfile_path, logger_name=None, labels_override=None):
        self.dockerfile_path = dockerfile_path
        self._setup_logger(logger_name)
        self._parser = None
        self._label_data = {}
        self._labels_override = labels_override or {}

    def _setup_logger(self, logger_name=None):
        if logger_name:
            dockerfile_parse.parser.logger = logging.getLogger("%s.dockerfile_parse"
                                                               % logger_name)

    def _parse(self):
        self._parser = dockerfile_parse.parser.DockerfileParser(self.dockerfile_path)

    def get_labels(self):
        """returns all labels how they are found in Dockerfile"""
        self._parse()
        labels = self._parser.labels
        if self._labels_override:
            labels.update(self._labels_override)
        return labels

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


class CreateContainerTask(BaseTaskHandler):
    Methods = ['createContainer']
    _taskWeight = 2.0

    def __init__(self, id, method, params, session, options, workdir=None):
        BaseTaskHandler.__init__(self, id, method, params, session, options,
                                 workdir)
        self._osbs = None

    def osbs(self):
        """Handler of OSBS object"""
        if not self._osbs:
            osbs.logger = logging.getLogger("%s.osbs" % self.logger.name)
            osbs.core.logger = logging.getLogger("%s.osbs.core" %
                                                 self.logger.name)
            osbs.api.logger = logging.getLogger("%s.osbs.api" %
                                                self.logger.name)
            osbs.http.logger = logging.getLogger("%s.osbs.http" %
                                                 self.logger.name)
            osbs.logger.debug("osbs logger installed")
            os_conf = Configuration()
            build_conf = Configuration()
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

    def handler(self, src, target_info, arch, scratch=False,
                yum_repourls=None, branch=None, push_url=None,
                labels=None):
        if not yum_repourls:
            yum_repourls = []
        if not labels:
            labels = {}

        this_task = self.session.getTaskInfo(self.id)
        self.logger.debug("This task: %r", this_task)
        owner_info = self.session.getUser(this_task['owner'])
        self.logger.debug("Started by %s", owner_info['name'])

        scm = My_SCM(src)
        scm.assert_allowed(self.options.allowed_scms)
        git_uri = scm.get_git_uri()
        component = scm.get_component()

        create_build_args = {
            'git_uri': git_uri,
            'git_ref': scm.revision,
            'user': owner_info['name'],
            'component': component,
            'target': target_info['name'],
            'architecture': arch,
            'yum_repourls': yum_repourls,
            'scratch': scratch,
            'koji_task_id': self.id,
        }
        if branch:
            create_build_args['git_branch'] = branch
        if push_url:
            create_build_args['git_push_url'] = push_url
        if labels:
            create_build_args['labels'] = labels
        build_response = self.osbs().create_build(
            **create_build_args
        )
        build_id = build_response.get_build_name()
        self.logger.debug("OSBS build id: %r", build_id)

        self.logger.debug("Waiting for osbs build_id: %s to be scheduled.",
                          build_id)
        # we need to wait for kubelet to schedule the build, otherwise it's 500
        self.osbs().wait_for_build_to_get_scheduled(build_id)
        self.logger.debug("Build was scheduled")

        osbs_logs_dir = self.resultdir()
        koji.ensuredir(osbs_logs_dir)
        pid = os.fork()
        if pid:
            self._incremental_upload_logs(pid)

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

        if response.is_failed():
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


class BuildContainerTask(BaseTaskHandler):
    Methods = ['buildContainer']
    # We mostly just wait on other tasks. Same value as for regular 'build'
    # method.
    _taskWeight = 0.2

    def __init__(self, id, method, params, session, options, workdir=None):
        BaseTaskHandler.__init__(self, id, method, params, session, options,
                                 workdir)
        self._osbs = None

    def osbs(self):
        """Handler of OSBS object"""
        if not self._osbs:
            os_conf = Configuration()
            build_conf = Configuration()
            self._osbs = OSBS(os_conf, build_conf)
            assert self._osbs
        return self._osbs

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

    def runBuilds(self, src, target_info, arches, scratch=False,
                  yum_repourls=None, branch=None, push_url=None,
                  labels=None):
        self.logger.debug("Spawning jobs for arches: %r" % (arches))
        subtasks = {}
        for arch in arches:
            if koji.util.multi_fnmatch(arch, self.options.literal_task_arches):
                taskarch = arch
            else:
                taskarch = koji.canonArch(arch)
            subtasks[arch] = self.session.host.subtask(method='createContainer',
                                                       arglist=[src,
                                                                target_info,
                                                                arch,
                                                                scratch,
                                                                yum_repourls,
                                                                branch,
                                                                push_url,
                                                                labels],
                                                       label='%s-container' % arch,
                                                       parent=self.id,
                                                       arch=taskarch)
        self.logger.debug("Got image subtasks: %r", (subtasks))
        self.logger.debug("Waiting on image subtasks...")
        results = self.wait(subtasks.values(), all=True, failany=True)

        self.logger.debug("Results: %r", results)
        return results

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

    def _get_labels_override(self, opts):
        labels = {}
        release = opts.get('release', None)
        if release:
            labels['release'] = release
        return labels

    def handler(self, src, target, opts=None):
        if not opts:
            opts = {}
        self.opts = opts
        data = {}

        self.event_id = self.session.getLastEvent()['id']
        target_info = self.session.getBuildTarget(target, event=self.event_id)
        build_tag = target_info['build_tag']
        archlist = self.getArchList(build_tag)

        dockerfile_path = self.fetchDockerfile(src)
        labels_override = self._get_labels_override(opts)
        labels_wrapper = LabelsWrapper(dockerfile_path,
                                       logger_name=self.logger.name,
                                       labels_override=labels_override)
        missing_labels = labels_wrapper.get_missing_label_ids()
        if missing_labels:
            formatted_labels_list = [labels_wrapper.format_label(label_id) for
                                     label_id in missing_labels]
            msg_template = ("Required LABELs haven't been found in "
                            "Dockerfile: %s.")
            raise koji.BuildError, (msg_template %
                                    ', '.join(formatted_labels_list))

        data = labels_wrapper.get_extra_data()
        admin_opts = self._get_admin_opts(opts)
        data.update(admin_opts)

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
                expected_nvr = labels_wrapper.get_expected_nvr()
                try:
                    build_id = self.session.getBuild(expected_nvr)['id']
                except:
                    self.logger.info("No build for %s found", expected_nvr, exc_info=True)
                else:
                    raise koji.BuildError(
                        "Build for %s already exists, id %s" % (expected_nvr, build_id))

            results = self.runBuilds(src, target_info, archlist,
                                     scratch=opts.get('scratch', False),
                                     yum_repourls=opts.get('yum_repourls', None),
                                     branch=opts.get('git_branch', None),
                                     push_url=opts.get('push_url', None),
                                     labels=labels_override,
                                     )
            all_repositories = []
            all_koji_builds = []
            for result in results.values():
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
