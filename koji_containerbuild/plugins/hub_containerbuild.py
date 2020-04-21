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

from __future__ import absolute_import

import sys
import logging

import koji
from koji.context import context
from koji.plugin import export

koji_hub_path = '/usr/share/koji-hub/'
sys.path.insert(0, koji_hub_path)
import kojihub  # noqa: E402 # pylint: disable=import-error

#logger = logging.getLogger('koji.plugins.containerbuild')
logger = logging.getLogger('koji.plugins')


def _get_task_opts_and_opts(opts, priority, channel):
    if opts is None:
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

    return opts, taskOpts

@export
def buildContainer(src, target, opts=None, priority=None, channel='container'):
    """Create a container build task

    :param str src: The source URI for this container.
    :param str target: The build target for this container.
    :param dict opts: Settings for this build, for example "scratch: true".
                      The full list of available options is in
                      builder_containerbuild.py.
    :param int priority: the amount to increase (or decrease) the task
                         priority, relative to the default priority; higher
                         values mean lower priority; only admins have the
                         right to specify a negative priority here
    :param str channel: the channel to allocate the task to (defaults to the
                        "container" channel)
    :returns: the task ID (integer)
    """
    new_opts, taskOpts = _get_task_opts_and_opts(opts, priority, channel)
    return kojihub.make_task('buildContainer', [src, target, new_opts], **taskOpts)


@export
def buildSourceContainer(target, opts=None, priority=None, channel='container'):
    """Create a source container build task

    :param str target: The build target for this container.
    :param dict opts: Settings for this build, for example "scratch: true".
                      The full list of available options is in
                      builder_containerbuild.py.
    :param int priority: the amount to increase (or decrease) the task
                         priority, relative to the default priority; higher
                         values mean lower priority; only admins have the
                         right to specify a negative priority here
    :param str channel: the channel to allocate the task to (defaults to the
                        "container" channel)
    :returns: the task ID (integer)
    """
    new_opts, taskOpts = _get_task_opts_and_opts(opts, priority, channel)
    return kojihub.make_task('buildSourceContainer', [target, new_opts], **taskOpts)
