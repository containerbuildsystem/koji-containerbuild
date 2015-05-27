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
import logging

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


class CreateContainerTask(BaseTaskHandler):
    Methods = ['createContainer']
    _taskWeight = 2.0

    def __init__(self, id, method, params, session, options, workdir=None):
        BaseTaskHandler.__init__(self, id, method, params, session, options,
                                 workdir)
        self._osbs = None

    def _download_logs(self, osbs_build_id):
        build_log = os.path.join(self.workdir, 'build.log')
        self.logger.debug("Getting logs from OSBS")
        build_log_contents = self.osbs().get_build_logs(osbs_build_id)
        self.logger.debug("Logs from OSBS retrieved")
        outfile = open(build_log, 'w')
        outfile.write(build_log_contents)
        outfile.close()
        self.logger.debug("Logs written to: %s" % build_log)
        return ['build.log']

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

    def handler(self, src, target_info, build_tag, arch, scratch=False):
        this_task = self.session.getTaskInfo(self.id)
        self.logger.debug("This task: %r", this_task)
        owner_info = self.session.getUser(this_task['owner'])
        self.logger.debug("Started by %s", owner_info['name'])

        scm = My_SCM(src)
        scm.assert_allowed(self.options.allowed_scms)
        git_uri = scm.get_git_uri()
        component = scm.get_component()

        build_response = self.osbs().create_build(
            git_uri=git_uri,
            git_ref=scm.revision,
            user=owner_info['name'],
            component=component,
            target=target_info['name'],
            architecture=arch,
        )
        build_id = build_response.build_id
        self.logger.debug("OSBS build id: %r", build_id)

        self.logger.info("Waiting for osbs build_id: %s to finish.", build_id)
        response = self.osbs().wait_for_build_to_finish(build_id)
        self.logger.debug("OSBS build finished with status: %s. Build "
                          "response: %s.", response.status,
                          response.json)
        logs = self._download_logs(build_id)

        rpmlist = []
        try:
            rpm_packages = response.get_rpm_packages()
        except (KeyError, TypeError), error:
            self.logger.error("Build response miss rpm-package: %s" % error)
            rpm_packages = ''
        if rpm_packages is None:
            rpm_packages = ''
        for package in rpm_packages.split('\n'):
            if len(package.strip()) == 0:
                continue
            parts = package.split(',')
            rpm_info = self._rpm_package_info(parts)
            if not rpm_info:
                continue
            rpmlist.append(rpm_info)

        repo_info = self.session.getRepo(target_info['build_tag'])
        # TODO: copied from image build
        # TODO: hack to make this work for now, need to refactor
        if scratch:
            br = kojid.BuildRoot(self.session, self.options, build_tag, arch,
                                 self.id, repo_id=repo_info['id'])
            br.markExternalRPMs(rpmlist)
            # TODO: I'm not sure if this is ok
            br.expire()

        containerdata = {
            'arch': arch,
            'task_id': self.id,
            'logs': logs,
            'osbs_build_id': build_id,
            'rpmlist': rpmlist,
        }

        # upload the build output
        for filename in containerdata['logs']:
            build_log = os.path.join(self.workdir, filename)
            self.uploadFile(build_log)

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

    def initContainerBuild(self, name, version, release, target_info, opts):
        """create a build object for this container build"""
        pkg_cfg = self.session.getPackageConfig(target_info['dest_tag_name'],
                                                name)
        self.logger.debug("%r" % pkg_cfg)
        if not opts.get('skip_tag') and not opts.get('scratch'):
            # Make sure package is on the list for this tag
            if pkg_cfg is None:
                raise koji.BuildError("package (container) %s not in list for tag %s" % (name, target_info['dest_tag_name']))
            elif pkg_cfg['blocked']:
                raise koji.BuildError("package (container)  %s is blocked for tag %s" % (name, target_info['dest_tag_name']))
        return self.session.host.initContainerBuild(self.id,
                                                    dict(name=name,
                                                         version=version,
                                                         release=release,
                                                         epoch=0))

    def runBuilds(self, src, target_info, build_tag, arches, scratch=False):
        subtasks = {}
        for arch in arches:
            subtasks[arch] = self.session.host.subtask(method='createContainer',
                                                       arglist=[src,
                                                                target_info,
                                                                build_tag,
                                                                arch,
                                                                scratch],
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

    def getRelease(self, name, ver):
        """return the next available release number for an N-V"""
        return self.session.getNextRelease(dict(name=name, version=ver))

    def handler(self, src, target, opts=None):
        if not opts:
            opts = {}
        self.opts = opts
        data = {}

        self.event_id = self.session.getLastEvent()['id']
        target_info = self.session.getBuildTarget(target, event=self.event_id)
        build_tag = target_info['build_tag']
        archlist = self.getArchList(build_tag)
        data['task_id'] = self.id

        # scratch builds do not get imported
        if not self.opts.get('scratch'):
            name = opts.get('name')
            version = opts.get('version')
            release = opts.get('release')

            if not name:
                scm = My_SCM(src)
                name = scm.get_component()
                if not name:
                    raise koji.BuildError('Name needs to be specified for non-scratch container builds')
            if not version:
                raise koji.BuildError('Version needs to be specified for non-scratch container builds')
            if not release:
                release = self.getRelease(name, version)
                if not release:
                    raise koji.BuildError('Release was not specified and failed to get one')

            bld_info = self.initContainerBuild(name, version, release,
                                               target_info, opts)

        try:
            self.extra_information = {"src": src, "data": data,
                                      "target": target}
            if not SCM.is_scm_url(src):
                raise koji.BuildError('Invalid source specification: %s' % src)
            results = self.runBuilds(src, target_info, build_tag, archlist,
                                     opts.get('scratch', False))
            results_xmlrpc = {}
            for task_id, result in results.items():
                # get around an xmlrpc limitation, use arches for keys instead
                results_xmlrpc[str(task_id)] = result
            for result in results.values():
                self._raise_if_image_failed(result['osbs_build_id'])
            if opts.get('scratch'):
                # scratch builds do not get imported
                self.session.host.moveContainerToScratch(self.id,
                                                         results_xmlrpc)
            else:
                self.session.host.completeContainerBuild(self.id,
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

    def _raise_if_image_failed(self, osbs_build_id):
        build = self.osbs().get_build(osbs_build_id)
        if build.is_failed():
            raise ContainerError('Image build failed')
