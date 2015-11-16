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


# List of LABELs to fetch from Dockerfile
LABELS = ('BZComponent', 'Version', 'Release', 'Architecture')


# Map from LABELS to extra data
LABEL_MAP = {
    'BZComponent': 'name',
    'Version': 'version',
    'Release': 'release',
    'Architecture': 'architecture',
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


class CreateContainerTask(BaseTaskHandler):
    Methods = ['createContainer']
    _taskWeight = 2.0

    def __init__(self, id, method, params, session, options, workdir=None):
        BaseTaskHandler.__init__(self, id, method, params, session, options,
                                 workdir)
        self._osbs = None

    def _download_logs(self, osbs_build_id):
        file_names = []
        fn = 'build.log'
        build_log = os.path.join(self.workdir, fn)
        self.logger.debug("Getting docker logs from OSBS")
        build_log_contents = self.osbs().get_docker_build_logs(osbs_build_id)
        self.logger.debug("Docker logs from OSBS retrieved")
        outfile = open(build_log, 'w')
        outfile.write(build_log_contents)
        outfile.close()
        self.logger.debug("Logs written to: %s" % build_log)
        file_names.append(fn)

        fn = 'openshift-final.log'
        build_log = os.path.join(self.workdir, fn)
        self.logger.debug("Getting OpenShift logs from OSBS")
        build_log_contents = self.osbs().get_build_logs(osbs_build_id,
                                                        follow=False)
        self.logger.debug("Docker logs from OSBS retrieved")
        outfile = open(build_log, 'w')
        outfile.write(build_log_contents)
        outfile.close()
        file_names.append(fn)

        return file_names

    def _get_file_url(self, source_filename):
        return "https://%s/image-export/%s/%s" % (self.osbs().os_conf.get_build_host(),
                                                  self.nfs_dest_dir(),
                                                  source_filename)

    def _download_files(self, source_filename, target_filename=None):
        if not target_filename:
            target_filename = "image.tar"
        localpath = os.path.join(self.workdir, target_filename)
        remote_url = self._get_file_url(source_filename)
        koji.ensuredir(self.workdir)
        verify_ssl = self.osbs().os_conf.get_verify_ssl()
        if verify_ssl:
            ssl_verify_peer = 1
            ssl_verify_host = 2
        else:
            ssl_verify_peer = 0
            ssl_verify_host = 0
        self.logger.debug("Going to download %r to %r.", remote_url, localpath)
        if isinstance(remote_url, unicode):
            remote_url = remote_url.encode('utf-8')
            self.logger.debug("remote_url changed to %r", remote_url)
        if isinstance(localpath, unicode):
            localpath = localpath.encode('utf-8')
            self.logger.debug("localpath changed to %r", localpath)
        try:
            output_filename = urlgrabber.urlgrab(remote_url,
                                                 filename=localpath,
                                                 ssl_verify_peer=ssl_verify_peer,
                                                 ssl_verify_host=ssl_verify_host)
        except urlgrabber.grabber.URLGrabError, error:
            self.logger.info("Failed to download file from URL %s: %s.",
                             remote_url, error)
            return []
        self.logger.debug("Output: %s.", output_filename)
        return [output_filename]

    def nfs_dest_dir(self):
        return "task-%s" % self.id

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
            build_conf = Configuration(nfs_dest_dir=self.nfs_dest_dir())
            self._osbs = OSBS(os_conf, build_conf)
            assert self._osbs
        return self._osbs

    def _rpm_package_info(self, parts):
        if len(parts) < 8:
            self.logger.error("Too few number of fields in the list"
                              " of rpms: %r", parts)
            return
        rpm = {
            'name': parts[0],
            'version': parts[1],
            'release': parts[2],
            'arch': parts[3],
            'size': int(parts[5]),
            'sigmd5': parts[6],
            'buildtime': int(parts[7])
        }

        if parts[4] == '(none)':
            rpm['epoch'] = None
        else:
            rpm['epoch'] = int(parts[4])
        return rpm

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

    def _get_rpm_packages(self, response):
        rpmlist = []
        try:
            rpm_packages = response.get_rpm_packages()
        except (KeyError, TypeError), error:
            self.logger.error("Build response miss rpm-package: %s" % error)
            rpm_packages = ''
        if rpm_packages is None:
            rpm_packages = ''
        self.logger.debug("List of rpms: %s", rpm_packages)
        for package in rpm_packages.split('\n'):
            if len(package.strip()) == 0:
                continue
            parts = package.split(',')
            rpm_info = self._rpm_package_info(parts)
            if not rpm_info:
                continue
            rpmlist.append(rpm_info)
        return rpmlist

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

    def handler(self, src, target_info, arch, output_template, scratch=False,
                yum_repourls=None, branch=None, push_url=None):
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

        create_build_args = {
            'git_uri': git_uri,
            'git_ref': scm.revision,
            'user': owner_info['name'],
            'component': component,
            'target': target_info['name'],
            'architecture': arch,
            'yum_repourls': yum_repourls,
        }
        if branch:
            create_build_args['git_branch'] = branch
        if push_url:
            create_build_args['git_push_url'] = push_url
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
        logs = self._download_logs(build_id)

        self.logger.info("Response status: %r", response.is_succeeded())

        files = []
        if response.is_succeeded() and response.get_tar_metadata_filename():
            files = self._download_files(response.get_tar_metadata_filename(),
                                         "image-%s.tar" % arch)

        rpmlist = []
        if response.is_succeeded():
            rpmlist = self._get_rpm_packages(response)

        self.logger.debug("rpm list:")
        for rpm in rpmlist:
            self.logger.debug("%(name)s-%(version)s-%(release)s.%(arch)s.rpm" %
                              rpm)

        repo_info = self.session.getRepo(target_info['build_tag'])
        # TODO: copied from image build
        # TODO: hack to make this work for now, need to refactor
        if not scratch:
            try:
                br = kojid.BuildRoot(self.session, self.options,
                                     target_info['build_tag'], arch,
                                     self.id, repo_id=repo_info['id'])
                br.markExternalRPMs(rpmlist)
                # TODO: I'm not sure if this is ok
                br.expire()
            except Exception, error:
                raise koji.PostBuildError("Failed to distuinguish external "
                                          "rpms: %s" % error)

        repositories = []
        if response.is_succeeded():
            repositories = self._get_repositories(response)

        self.logger.info("Image available in the following repositories: %r",
                         repositories)

        containerdata = {
            'arch': arch,
            'task_id': self.id,
            'logs': logs,
            'osbs_build_id': build_id,
            'rpmlist': rpmlist,
            'files': [],
            'repositories': repositories
        }

        # upload the build output
        for filename in containerdata['logs']:
            build_log = os.path.join(self.workdir, filename)
            self.uploadFile(build_log)

        if response.is_succeeded() and len(files) != 1:
            self.logger.error("There should be only one container file but "
                              "there are %d: %s", len(files), files)
        else:
            for filename in files:
                full_path = os.path.join(self.workdir, filename)
                self.uploadFile(full_path, remoteName=output_template)
                containerdata['files'].append(os.path.basename(output_template))

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

    def runBuilds(self, src, target_info, arches, output_template,
                  scratch=False, yum_repourls=None, branch=None, push_url=None):
        subtasks = {}
        for arch in arches:
            subtasks[arch] = self.session.host.subtask(method='createContainer',
                                                       arglist=[src,
                                                                target_info,
                                                                arch,
                                                                output_template,
                                                                scratch,
                                                                yum_repourls,
                                                                branch,
                                                                push_url],
                                                       label='container',
                                                       parent=self.id)
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
            #XXX - need to handle this better
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

    def _getOutputImageTemplate(self, **kwargs):
        output_template = 'image.tar'
        have_all_fields = True
        for field in ['name', 'version', 'release', 'architecture']:
            if field not in kwargs:
                self.logger.info("Missing var for output image name: %s",
                                 field)
                have_all_fields = False
        if have_all_fields:
            output_template = "%(name)s-%(version)s-%(release)s-%(architecture)s.tar" % kwargs
        return output_template

    def _get_admin_opts(self, opts):
        epoch = opts.get('epoch', 0)
        if epoch:
            self.session.assertPerm('admin')

        return {'epoch': epoch}

    def _get_dockerfile_labels(self, dockerfile_path, fields):
        """Roughly corresponds to koji.get_header_fields()

        It differs from get_header_fields() which is ran on rpms that missing
        fields are not considered to be error.
        """
        dockerfile_parse.parser.logger = logging.getLogger("%s.dockerfile_parse"
                                                           % self.logger.name)
        parser = dockerfile_parse.parser.DockerfileParser(dockerfile_path)
        ret = {}
        for f in fields:
            try:
                ret[f] = parser.labels[f]
            except KeyError:
                self.logger.info("No such label: %s", f)
        return ret

    def _map_labels_to_data(self, labels):
        data = {}
        for key, value in labels.items():
            if key in LABEL_MAP:
                key = LABEL_MAP[key]
            data[key] = value
        return data

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
        data_labels = self._get_dockerfile_labels(dockerfile_path, LABELS)
        data = self._map_labels_to_data(data_labels)

        admin_opts = self._get_admin_opts(opts)
        data.update(admin_opts)

        for field in ['name', 'version', 'release', 'architecture']:
            if field not in data:
                raise koji.BuildError('%s needs to be specified for '
                                      'container builds' % field)

        # scratch builds do not get imported
        if not self.opts.get('scratch'):

            if not opts.get('skip_tag'):
                self.check_whitelist(data['name'], target_info)
            bld_info = self.session.host.initImageBuild(self.id, data)
        try:
            self.extra_information = {"src": src, "data": data,
                                      "target": target}
            if not SCM.is_scm_url(src):
                raise koji.BuildError('Invalid source specification: %s' % src)
            output_template = self._getOutputImageTemplate(**data)
            results = self.runBuilds(src, target_info, archlist,
                                     output_template,
                                     opts.get('scratch', False),
                                     opts.get('yum_repourls', None),
                                     opts.get('git_branch', None),
                                     opts.get('push_url', None))
            results_xmlrpc = {}
            for task_id, result in results.items():
                # get around an xmlrpc limitation, use arches for keys instead
                results_xmlrpc[str(task_id)] = result
            all_repositories = []
            for result in results.values():
                self._raise_if_image_failed(result['osbs_build_id'])
                try:
                    repository = result.get('repositories')
                    all_repositories.extend(repository)
                except Exception, error:
                    self.logger.error("Failed to merge list of repositories "
                                      "%r. Reason (%s): %s", repository,
                                      type(error), error)
            if opts.get('scratch'):
                # scratch builds do not get imported
                self.session.host.moveImageBuildToScratch(self.id,
                                                          results_xmlrpc)
            else:
                self.session.host.completeImageBuild(self.id,
                                                     bld_info['id'],
                                                     results_xmlrpc)
        except (SystemExit, ServerExit, KeyboardInterrupt):
            # we do not trap these
            raise
        except:
            if not self.opts.get('scratch'):
                # scratch builds do not get imported
                if bld_info:
                    self.session.host.failBuild(self.id, bld_info['id'])
            # reraise the exception
            raise

        # tag it
        if not opts.get('scratch') and not opts.get('skip_tag'):
            tag_task_id = self.session.host.subtask(
                method='tagBuild',
                arglist=[target_info['dest_tag'], bld_info['id'], False, None,
                         True],
                label='tag',
                parent=self.id,
                arch='noarch')
            self.wait(tag_task_id)

        report = ('Image available in following repositories:\n%s' %
                  '\n'.join(all_repositories))
        return report

    def _raise_if_image_failed(self, osbs_build_id):
        build = self.osbs().get_build(osbs_build_id)
        if build.is_failed():
            raise ContainerError('Image build failed. OSBS build id: %s' %
                                 osbs_build_id)
