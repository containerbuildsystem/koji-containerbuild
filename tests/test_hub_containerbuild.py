"""
Copyright (C) 2019  Red Hat, Inc.

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 2.1 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA
"""
from __future__ import absolute_import

import koji
import pytest
from flexmock import flexmock

from koji_containerbuild.plugins import hub_containerbuild


def mocked_koji_context(admin_perms=False):
    """
    Mock koji context for testing hub_containerbuild (replace
    hub_containerbuild.context with the result of this function).

    :param admin_perms: Does the user have admin permissions?
    :return: A mock object to replace hub_containerbuild.context with.
    """
    session = flexmock()
    (session
        .should_receive('hasPerm')
        .with_args('admin')
        .and_return(admin_perms))

    context = flexmock(session=session)
    return context


def mocked_kojihub_for_task(src, target, opts,
                            priority=None, channel='container',
                            should_receive_task=True, build_type='buildContainer'):
    """
    Mock koji-hub for testing hub_containerbuild (replace
    hub_containerbuild.kojihub with the result of this function).

    :param src: 1st argument for a 'buildContainer' task
    :param target: 2nd argument for a 'buildContainer' task
    :param opts: 3rd argument for a 'buildContainer' task
    :param priority: keyword argument for `make_task`,
                     is expected only if not None, not 0
    :param channel: keyword argument for `make_task`,
                    is expected only if not None, not empty
    :param should_receive_task: Should kojihub receive the call to `make_task`?
    :param build_type: containerbuild or sourcecontainerbuild

    :return: A mock object to replace hub_containerbuild.kojihub with.
    """
    task_opts = {}
    if channel:
        task_opts['channel'] = channel
    if priority:
        task_opts['priority'] = koji.PRIO_DEFAULT + priority

    kojihub = flexmock()
    if build_type == 'buildContainer':
        (kojihub
            .should_receive('make_task')
            .with_args('buildContainer',
                       [src, target, opts],
                       **task_opts)
            .times(1 if should_receive_task else 0))
    elif build_type == 'buildSourceContainer':
        (kojihub
            .should_receive('make_task')
            .with_args('buildSourceContainer',
                       [target, opts],
                       **task_opts)
            .times(1 if should_receive_task else 0))

    return kojihub


@pytest.mark.parametrize('build_type', ['buildContainer', 'buildSourceContainer'])
@pytest.mark.parametrize('admin_perms', [True, False])
@pytest.mark.parametrize('priority', [1, 0, None, -1])
def test_priority_permissions(monkeypatch, build_type, priority, admin_perms):
    src, target = 'source', 'target'

    context = mocked_koji_context(admin_perms)
    monkeypatch.setattr(hub_containerbuild, 'context', context)

    should_succeed = priority is None or priority >= 0 or admin_perms

    kojihub = mocked_kojihub_for_task(src, target, {},
                                      priority=priority,
                                      should_receive_task=should_succeed,
                                      build_type=build_type)
    monkeypatch.setattr(hub_containerbuild, 'kojihub', kojihub)

    if should_succeed:
        if build_type == 'buildContainer':
            hub_containerbuild.buildContainer(src, target, priority=priority)
        elif build_type == 'buildSourceContainer':
            hub_containerbuild.buildSourceContainer(target, priority=priority)

    else:
        with pytest.raises(koji.ActionNotAllowed) as exc_info:
            if build_type == 'buildContainer':
                hub_containerbuild.buildContainer(src, target, priority=priority)
            elif build_type == 'buildSourceContainer':
                hub_containerbuild.buildSourceContainer(target, priority=priority)

        e = exc_info.value
        assert str(e) == 'only admins may create high-priority tasks'
