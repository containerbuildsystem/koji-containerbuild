"""Koji hub plugin extends Koji to manage container builds"""

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
import sys
import inspect
import logging
import warnings

import koji
from koji.context import context
from koji.plugin import export, export_in

sys.path.insert(0, '/usr/share/koji-hub/')
import kojihub
#kojihub = inspect.getmodule(sys._getframe(2)).kojihub

#logger = logging.getLogger('koji.plugins.containerbuild')
logger = logging.getLogger('koji.plugins')


# TODO: This monkey patch won't work for other koji calls (e.g. from CLI)
def pathinfo_containerbuild(self, build):
    """Return the directory where the containers for the build are stored"""
    return self.build(build) + '/containers'

koji.pathinfo.containerbuild = pathinfo_containerbuild


@export
def buildContainer(src, target, opts=None, priority=None, channel='container'):
    """Create a container build task

    target: the build target
    priority: the amount to increase (or decrease) the task priority, relative
              to the default priority; higher values mean lower priority; only
              admins have the right to specify a negative priority here
    channel: the channel to allocate the task to (defaults to the "container"
             channel)

    Returns the task ID
    """
    if not opts:
        opts = {}
    taskOpts = {}
    if priority:
        if priority < 0:
            if not context.session.hasPerm('admin'):
                raise koji.ActionNotAllowed('only admins may create'
                                            ' high-priority tasks')
        taskOpts['priority'] = koji.PRIO_DEFAULT + priority
    if channel:
        taskOpts['channel'] = channel
    return kojihub.make_task('buildContainer', [src, target, opts], **taskOpts)


@export_in(module='host')
def moveContainerToScratch(task_id, results):
    """move a completed image scratch build into place"""
    host = kojihub.Host()
    host.verify()
    task = kojihub.Task(task_id)
    task.assertHost(host.id)

    scratchdir = koji.pathinfo.scratch()
    logger.debug('scratch image results: %s', results)
    for sub_results in results.values():
        sub_task_id = sub_results['task_id']
        sub_task = kojihub.Task(sub_task_id)
        workdir = koji.pathinfo.task(sub_task_id)
        username = kojihub.get_user(sub_task.getOwner())['name']
        destdir = os.path.join(scratchdir, username, 'task_%s' % task_id)
        koji.ensuredir(destdir)
        for img in sub_results['logs']:
            src = os.path.join(workdir, img)
            logger.debug('Source log: %s', img)
            # verify files exist
            if not os.path.exists(src):
                raise koji.GenericError("no such file: %s" % src)
            dest = os.path.join(destdir, img)
            logger.debug('renaming %s to %s', src, dest)
            os.rename(src, dest)
            os.symlink(dest, src)


@export_in(module='host')
def initContainerBuild(task_id, build_info):
    """create a new in-progress container build"""
    host = kojihub.Host()
    host.verify()
    task = kojihub.Task(task_id)
    task.assertHost(host.id)
    data = build_info.copy()
    data['task_id'] = task_id
    data['owner'] = task.getOwner()
    data['state'] = koji.BUILD_STATES['BUILDING']
    data['completion_time'] = None
    build_id = kojihub.new_build(data)
    data['id'] = build_id
    new_container_build(data)
    return data


def new_container_build(build_info):
    """
    Added container metadata to an existing build. This is just the buildid so
    that we can distinguish container builds from other types.
    """
    # We don't have to worry about updating an container build because the id
    # is the only thing we care about, and that should never change if a build
    # fails first and succeeds later on a resubmission.
    query = kojihub.QueryProcessor(tables=('container_builds',),
                                   columns=('build_id',),
                                   clauses=('build_id = %(build_id)i',),
                                   values={'build_id': build_info['id']})
    result = query.executeOne()
    if not result:
        insert = kojihub.InsertProcessor('container_builds')
        insert.set(build_id=build_info['id'])
        insert.execute()


@export_in(module='host')
def completeContainerBuild(task_id, build_id, results):
    """Set an container build to the COMPLETE state"""
    host = kojihub.Host()
    host.verify()
    task = kojihub.Task(task_id)
    task.assertHost(host.id)
    importContainer(task_id, build_id, results)

    st_complete = koji.BUILD_STATES['COMPLETE']
    update = kojihub.UpdateProcessor('build', clauses=['id=%(build_id)i'],
                                     values={'build_id': build_id})
    update.set(id=build_id, state=st_complete)
    update.rawset(completion_time='now()')
    update.execute()
    # send email
    kojihub.build_notification(task_id, build_id)


def importContainer(task_id, build_id, results):
    """
    Import a built container, populating the database with metadata and
    moving the container to its final location.
    """
    for sub_results in results.values():
        importContainerInternal(task_id, build_id, sub_results)


# TODO: heavily hacked version of kojihub import_archive which accepts empty
# filepath. Do this in some less hackish way but still support workflow of
# later promotion of docker image.
def import_archive(filepath, buildinfo, type, typeInfo, buildroot_id=None):
    """
    Import an archive file and associate it with a build.  The archive can
    be any non-rpm filetype supported by Koji or None if the file doesn't exist
    (yet) and we need to reference it.

    filepath: full path to the archive file or None
    buildinfo: dict of information about the build to associate the archive with (as returned by getBuild())
    type: type of the archive being imported.  Currently supported archive types: maven, win, image
    typeInfo: dict of type-specific information
    buildroot_id: the id of the buildroot the archive was built in (may be null)
    """
    if filepath and not os.path.exists(filepath):
        raise koji.GenericError, 'no such file: %s' % filepath

    archiveinfo = {'buildroot_id': buildroot_id}
    if filepath:
        filename = koji.fixEncoding(os.path.basename(filepath))
        archiveinfo['filename'] = filename
        archivetype = kojihub.get_archive_type(filename, strict=True)
    else:
        archiveinfo['filename'] = ''
        archivetype = kojihub.get_archive_type(type_name='container',
                                               strict=True)
    archiveinfo['type_id'] = archivetype['id']
    archiveinfo['build_id'] = buildinfo['id']
    if filepath:
        archiveinfo['size'] = os.path.getsize(filepath)
    else:
        archiveinfo['size'] = 0

    if filepath:
        archivefp = file(filepath)
        m = koji.util.md5_constructor()
        while True:
            contents = archivefp.read(8192)
            if not contents:
                break
            m.update(contents)
        archivefp.close()
        archiveinfo['checksum'] = m.hexdigest()
        archiveinfo['checksum_type'] = koji.CHECKSUM_TYPES['md5']
    else:
        archiveinfo['checksum'] = ''
        archiveinfo['checksum_type'] = 0

    koji.plugin.run_callbacks('preImport', type='archive', archive=archiveinfo,
                              build=buildinfo, build_type=type,
                              filepath=filepath)

    # XXX verify that the buildroot is associated with a task that's associated with the build
    archive_id = kojihub._singleValue("SELECT nextval('archiveinfo_id_seq')",
                                      strict=True)
    archiveinfo['id'] = archive_id
    insert = kojihub.InsertProcessor('archiveinfo', data=archiveinfo)
    insert.execute()

    if type == 'container':
        insert = kojihub.InsertProcessor('image_archives')
        insert.set(archive_id=archive_id)
        insert.set(arch=typeInfo['arch'])
        insert.execute()
        # TODO this will be used if we really have file name
        if filepath:
            imgdir = os.path.join(koji.pathinfo.containerbuild(buildinfo))
            kojihub._import_archive_file(filepath, imgdir)
        # import log files?
    else:
        raise koji.BuildError, 'unsupported archive type: %s' % type

    archiveinfo = kojihub.get_archive(archive_id, strict=True)
    koji.plugin.run_callbacks('postImport', type='archive',
                              archive=archiveinfo, build=buildinfo,
                              build_type=type, filepath=filepath)
    return archiveinfo


def importContainerInternal(task_id, build_id, containerdata):
    """
    Import container info and the listing into the database, and move an
    container to the final resting place. The filesize may be reported as a
    string if it exceeds the 32-bit signed integer limit. This function will
    convert it if need be. This is the completeBuild for containers; it should
    not be called for scratch container builds.

    containerdata is:
    arch - the arch if the container
    files - files associated with the container
    rpmlist - the list of RPM NVRs installed into the container
    """
    host = kojihub.Host()
    host.verify()
    task = kojihub.Task(task_id)
    task.assertHost(host.id)

    koji.plugin.run_callbacks('preImport', type='container',
                              container=containerdata)

    # import the build output
    build_info = kojihub.get_build(build_id, strict=True)
    containerdata['relpath'] = koji.pathinfo.taskrelpath(containerdata['task_id'])
    archives = []
    archives.append(import_archive(None, build_info, 'container',
                                   containerdata))

    # record all of the RPMs installed in the containers
    # verify they were built in Koji or in an external repo
    rpm_ids = []
    for an_rpm in containerdata['rpmlist']:
        location = an_rpm.get('location')
        if location:
            data = kojihub.add_external_rpm(an_rpm, location, strict=False)
        else:
            data = kojihub.get_rpm(an_rpm, strict=True)
        rpm_ids.append(data['id'])

    # associate those RPMs with the container
    q = """INSERT INTO container_listing (container_id,rpm_id)
           VALUES (%(container_id)i,%(rpm_id)i)"""
    for archive in archives:
        sys.stderr.write('working on archive %s' % archive)
        if archive['filename'].endswith('xml'):
            continue
        logger.info('associating installed rpms with %s' % archive['id'])
        for rpm_id in rpm_ids:
            kojihub._dml(q, {'container_id': archive['id'], 'rpm_id': rpm_id})

    koji.plugin.run_callbacks('postImport', type='container',
                              container=containerdata, fullpath=None)
