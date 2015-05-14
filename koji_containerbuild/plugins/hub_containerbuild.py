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
        if sub_results.has_key('rpmresults'):
            rpm_results = sub_results['rpmresults']
            kojihub._import_wrapper(rpm_results['task_id'],
                                    kojihub.get_build(build_id, strict=True),
                                    rpm_results)


def importContainerInternal(task_id, build_id, imgdata):
    warnings.warn("importImageInternal() not yet implemented")
