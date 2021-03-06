"""
Copyright (c) 2019 Red Hat, Inc
All rights reserved.

This software may be modified and distributed under the terms
of the BSD license. See the LICENSE file for details.
"""

from textwrap import dedent
import sys

from flexmock import flexmock
import pytest

import atomic_reactor.utils.koji as koji_util
from atomic_reactor import util
from atomic_reactor.utils.cachito import CachitoAPI
from atomic_reactor.constants import PLUGIN_BUILD_ORCHESTRATE_KEY
from atomic_reactor.inner import DockerBuildWorkflow
from atomic_reactor.plugin import PreBuildPluginsRunner, PluginFailedException
from atomic_reactor.plugins import pre_reactor_config
from atomic_reactor.plugins.build_orchestrate_build import (
    WORKSPACE_KEY_OVERRIDE_KWARGS, OrchestrateBuildPlugin)
from atomic_reactor.plugins.pre_reactor_config import (
    ReactorConfigPlugin, WORKSPACE_CONF_KEY, ReactorConfig)
from atomic_reactor.plugins.pre_resolve_remote_source import ResolveRemoteSourcePlugin
from atomic_reactor.source import SourceConfig

from tests.constants import MOCK_SOURCE
from tests.stubs import StubInsideBuilder, StubSource


KOJI_HUB = 'http://koji.com/hub'
KOJI_TASK_ID = 123
KOJI_TASK_OWNER = 'spam'

CACHITO_URL = 'https://cachito.example.com'
CACHITO_REQUEST_ID = 98765
CACHITO_REQUEST_DOWNLOAD_URL = '{}/api/v1/{}/download'.format(CACHITO_URL, CACHITO_REQUEST_ID)
CACHITO_REQUEST_CONFIG_URL = '{}/api/v1/requests/{}/configuration-files'.format(
    CACHITO_URL,
    CACHITO_REQUEST_ID
)
CACHITO_ICM_URL = '{}/api/v1/requests/{}/content-manifest'.format(
    CACHITO_URL,
    CACHITO_REQUEST_ID
)

REMOTE_SOURCE_REPO = 'https://git.example.com/team/repo.git'
REMOTE_SOURCE_REF = 'b55c00f45ec3dfee0c766cea3d395d6e21cc2e5a'
REMOTE_SOURCE_PACKAGES = [
        {
            'name': 'test-package',
            'type': 'npm',
            'version': '0.0.1'
        }
    ]

CACHITO_SOURCE_REQUEST = {
    'id': CACHITO_REQUEST_ID,
    'repo': REMOTE_SOURCE_REPO,
    'ref': REMOTE_SOURCE_REF,
    'environment_variables': {
        'GO111MODULE': 'on',
        'GOPATH': 'deps/gomod',
        'GOCACHE': 'deps/gomod',
    },
    'flags': ['enable-confeti', 'enable-party-popper'],
    'pkg_managers': ['gomod'],
    'dependencies': [
        {
            'name': 'github.com/op/go-logging',
            'type': 'gomod',
            'version': 'v0.1.1',
        }
    ],
    'packages': [
        {
            'name': 'github.com/spam/bacon/v2',
            'type': 'gomod',
            'version': 'v2.0.3'
        }
    ],
    'configuration_files': CACHITO_REQUEST_CONFIG_URL,
    'content_manifest': CACHITO_ICM_URL,
    'extra_cruft': 'ignored',
}

REMOTE_SOURCE_JSON = {
    'repo': REMOTE_SOURCE_REPO,
    'ref': REMOTE_SOURCE_REF,
    'environment_variables': {
        'GO111MODULE': 'on',
        'GOPATH': 'deps/gomod',
        'GOCACHE': 'deps/gomod',
    },
    'flags': ['enable-confeti', 'enable-party-popper'],
    'pkg_managers': ['gomod'],
    'dependencies': [
        {
            'name': 'github.com/op/go-logging',
            'type': 'gomod',
            'version': 'v0.1.1',
        }
    ],
    'packages': [
        {
            'name': 'github.com/spam/bacon/v2',
            'type': 'gomod',
            'version': 'v2.0.3'
        }
    ],
    'configuration_files': CACHITO_REQUEST_CONFIG_URL,
    'content_manifest': CACHITO_ICM_URL,
}

CACHITO_ENV_VARS_JSON = {
    'GO111MODULE': {'kind': 'literal', 'value': 'on'},
    'GOPATH': {'kind': 'path', 'value': 'deps/gomod'},
    'GOCACHE': {'kind': 'path', 'value': 'deps/gomod'},
}

# Assert this with the CACHITO_ENV_VARS_JSON
CACHITO_BUILD_ARGS = {
    'GO111MODULE': 'on',
    'GOPATH': '/remote-source/deps/gomod',
    'GOCACHE': '/remote-source/deps/gomod',
    'CACHITO_ENV_FILE': '/remote-source/cachito.env',
}


@pytest.fixture
def workflow(tmpdir, user_params):
    workflow = DockerBuildWorkflow(source=MOCK_SOURCE)

    # Stash the tmpdir in workflow so it can be used later
    workflow._tmpdir = tmpdir

    class MockSource(StubSource):

        def __init__(self, workdir):
            super(MockSource, self).__init__()
            self.workdir = workdir

    workflow.source = MockSource(str(tmpdir))

    builder = StubInsideBuilder().for_workflow(workflow)
    builder.set_df_path(str(tmpdir))
    builder.tasker = flexmock()
    workflow.builder = flexmock(builder)
    workflow.buildstep_plugins_conf = [{'name': PLUGIN_BUILD_ORCHESTRATE_KEY}]

    mock_repo_config(workflow)
    mock_reactor_config(workflow)
    mock_build_json()
    mock_cachito_api(workflow)
    mock_koji()

    return workflow


def mock_reactor_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            version: 1
            cachito:
               api_url: {}
               auth:
                   ssl_certs_dir: {}
            koji:
                hub_url: /
                root_url: ''
                auth: {{}}
            """.format(CACHITO_URL, workflow._tmpdir))

    workflow.plugin_workspace[ReactorConfigPlugin.key] = {}

    workflow._tmpdir.join('cert').write('')
    config = util.read_yaml(data, 'schemas/config.json')

    workflow.plugin_workspace[ReactorConfigPlugin.key][WORKSPACE_CONF_KEY] = ReactorConfig(config)


def mock_build_json(build_json=None):
    if build_json is None:
        build_json = {'metadata': {'labels': {'koji-task-id': str(KOJI_TASK_ID)}}}
    flexmock(util).should_receive('get_build_json').and_return(build_json)


def mock_repo_config(workflow, data=None):
    if data is None:
        data = dedent("""\
            remote_source:
                repo: {}
                ref: {}
            """.format(REMOTE_SOURCE_REPO, REMOTE_SOURCE_REF))

    workflow._tmpdir.join('container.yaml').write(data)

    # The repo config is read when SourceConfig is initialized. Force
    # reloading here to make usage easier.
    workflow.source.config = SourceConfig(str(workflow._tmpdir))


def mock_cachito_api(workflow, user=KOJI_TASK_OWNER, source_request=None,
                     dependency_replacements=None,
                     env_vars_json=None):
    if source_request is None:
        source_request = CACHITO_SOURCE_REQUEST
    (flexmock(CachitoAPI)
        .should_receive('request_sources')
        .with_args(
            repo=REMOTE_SOURCE_REPO,
            ref=REMOTE_SOURCE_REF,
            user=user,
            dependency_replacements=dependency_replacements,
         )
        .and_return({'id': CACHITO_REQUEST_ID}))

    (flexmock(CachitoAPI)
        .should_receive('wait_for_request')
        .with_args({'id': CACHITO_REQUEST_ID})
        .and_return(source_request))

    (flexmock(CachitoAPI)
        .should_receive('download_sources')
        .with_args(source_request, dest_dir=str(workflow._tmpdir))
        .and_return(expected_dowload_path(workflow)))

    (flexmock(CachitoAPI)
        .should_receive('assemble_download_url')
        .with_args(source_request)
        .and_return(CACHITO_REQUEST_DOWNLOAD_URL))

    (flexmock(CachitoAPI)
        .should_receive('get_request_env_vars')
        .with_args(source_request['id'])
        .and_return(env_vars_json or CACHITO_ENV_VARS_JSON))


def mock_koji(user=KOJI_TASK_OWNER):
    koji_session = flexmock()
    flexmock(pre_reactor_config).should_receive('get_koji_session').and_return(koji_session)
    flexmock(koji_util).should_receive('get_koji_task_owner').and_return({'name': user})


def expected_dowload_path(workflow):
    return workflow._tmpdir.join('source.tar.gz')


def setup_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


def teardown_function(*args):
    # IMPORTANT: This needs to be done to ensure mocks at the module
    # level are reset between test cases.
    sys.modules.pop('pre_resolve_remote_source', None)


@pytest.mark.parametrize('scratch', (True, False))
@pytest.mark.parametrize('dr_strs, dependency_replacements',
                         ((None, None),
                          (['gomod:foo.bar/project:2'],
                           [{
                             'name': 'foo.bar/project',
                             'type': 'gomod',
                             'version': '2'}]),
                          (['gomod:foo.bar/project:2:newproject'],
                          [{
                            'name': 'foo.bar/project',
                            'type': 'gomod',
                            'new_name': 'newproject',
                            'version': '2'}]),
                          (['gomod:foo.bar/project'], None)))
@pytest.mark.parametrize('env_vars_json, expected_build_args', [
    [CACHITO_ENV_VARS_JSON, CACHITO_BUILD_ARGS],
    [
        {
            'GOPATH': {'kind': 'path', 'value': 'deps/gomod'},
            'GOCACHE': {'kind': 'path', 'value': 'deps/gomod'},
        },
        {
            'GOPATH': '/remote-source/deps/gomod',
            'GOCACHE': '/remote-source/deps/gomod',
            'CACHITO_ENV_FILE': '/remote-source/cachito.env',
        },
    ],
    [
        {'GO111MODULE': {'kind': 'literal', 'value': 'on'}},
        {
            'GO111MODULE': 'on',
            'CACHITO_ENV_FILE': '/remote-source/cachito.env',
        },
    ],
])
def test_resolve_remote_source(workflow, scratch, dr_strs, dependency_replacements,
                               env_vars_json, expected_build_args):
    build_json = {'metadata': {'labels': {'koji-task-id': str(KOJI_TASK_ID)}}}
    mock_build_json(build_json=build_json)
    mock_cachito_api(workflow,
                     dependency_replacements=dependency_replacements,
                     env_vars_json=env_vars_json)
    workflow.user_params['scratch'] = scratch
    err = None
    if dr_strs and not scratch:
        err = 'Cachito dependency replacements are only allowed for scratch builds'

    if dr_strs and any(len(dr.split(':')) < 3 for dr in dr_strs):
        err = 'Cachito dependency replacements must be'

    run_plugin_with_args(
        workflow,
        dependency_replacements=dr_strs,
        expect_error=err,
        expected_build_args=expected_build_args,
    )


@pytest.mark.parametrize(
    'env_vars_json',
    [
        {
            'GOPATH': {'kind': 'path', 'value': 'deps/gomod'},
            'GOCACHE': {'kind': 'path', 'value': 'deps/gomod'},
            'GO111MODULE': {'kind': 'literal', 'value': 'on'},
            'GOX': {'kind': 'new', 'value': 'new-kind'},
        },
    ]
)
def test_fail_build_if_unknown_kind(workflow, env_vars_json):
    mock_cachito_api(workflow, env_vars_json=env_vars_json)
    run_plugin_with_args(workflow, expect_error=r'.*Unknown kind new got from Cachito')


@pytest.mark.parametrize('build_json', ({}, {'metadata': {}}))
def test_no_koji_user(workflow, build_json, caplog):
    reactor_config = dedent("""\
        version: 1
        cachito:
           api_url: {}
           auth:
               ssl_certs_dir: {}
        koji:
            hub_url: /
            root_url: ''
            auth: {{}}
        """.format(CACHITO_URL, workflow._tmpdir))
    mock_reactor_config(workflow, reactor_config)
    mock_build_json(build_json=build_json)
    mock_cachito_api(workflow, user='unknown_user')
    log_msg = 'No build metadata'
    if build_json:
        log_msg = 'Invalid Koji task ID'
    run_plugin_with_args(workflow)
    assert log_msg in caplog.text


@pytest.mark.parametrize('pop_key', ('repo', 'ref', 'packages'))
def test_invalid_remote_source_structure(workflow, pop_key):
    source_request = {
        'id': CACHITO_REQUEST_ID,
        'repo': REMOTE_SOURCE_REPO,
        'ref': REMOTE_SOURCE_REF,
        'packages': REMOTE_SOURCE_PACKAGES,
    }
    source_request.pop(pop_key)
    mock_cachito_api(workflow, source_request=source_request)
    run_plugin_with_args(workflow, expect_error='Received invalid source request')


def test_ignore_when_missing_cachito_config(workflow):
    reactor_config = dedent("""\
        version: 1
        koji:
            hub_url: /
            root_url: ''
            auth: {}
        """)
    mock_reactor_config(workflow, reactor_config)
    result = run_plugin_with_args(workflow, expect_result=False)
    assert result is None


def test_invalid_cert_reference(workflow):
    bad_certs_dir = str(workflow._tmpdir.join('invalid-dir'))
    reactor_config = dedent("""\
        version: 1
        cachito:
           api_url: {}
           auth:
               ssl_certs_dir: {}
        koji:
            hub_url: /
            root_url: ''
            auth: {{}}
        """.format(CACHITO_URL, bad_certs_dir))
    mock_reactor_config(workflow, reactor_config)
    run_plugin_with_args(workflow, expect_error="Cachito ssl_certs_dir doesn't exist")


def test_ignore_when_missing_remote_source_config(workflow):
    remote_source_config = dedent("""---""")
    mock_repo_config(workflow, remote_source_config)
    result = run_plugin_with_args(workflow, expect_result=False)
    assert result is None


@pytest.mark.parametrize(('build_json', 'log_entry'), (
    ({}, 'No build metadata'),
    ({'metadata': None}, 'Invalid Koji task ID'),
    ({'metadata': {}}, 'Invalid Koji task ID'),
    ({'metadata': {'labels': {}}}, 'Invalid Koji task ID'),
    ({'metadata': {'labels': {'koji-task-id': None}}}, 'Invalid Koji task ID'),
    ({'metadata': {'labels': {'koji-task-id': 'not-an-int'}}}, 'Invalid Koji task ID'),
))
def test_bad_build_metadata(workflow, build_json, log_entry, caplog):
    mock_build_json(build_json=build_json)
    mock_cachito_api(workflow, user='unknown_user')
    run_plugin_with_args(workflow)
    assert log_entry in caplog.text
    assert 'unknown_user' in caplog.text


def test_remote_sources_in_config_fail(workflow):
    container_yaml_config = dedent("""\
            remote_sources:
            - name: a-remote-source
              remote-_source:
                repo: https://some.repo/here.git
                ref: e1be527f39ec31323f0454f7d1422c6260b00580
            """)
    err_msg = (
        "Multiple remote sources are not supported, "
        "use single remote source in container.yaml"
    )
    mock_repo_config(workflow, data=container_yaml_config)
    result = run_plugin_with_args(workflow, expect_result=False, expect_error=err_msg)
    assert result is None


def run_plugin_with_args(workflow, dependency_replacements=None, expect_error=None,
                         expect_result=True, expected_build_args=None):
    runner = PreBuildPluginsRunner(
        workflow.builder.tasker,
        workflow,
        [
            {'name': ResolveRemoteSourcePlugin.key,
             'args': {'dependency_replacements': dependency_replacements}},
        ]
    )

    if expect_error:
        with pytest.raises(PluginFailedException, match=expect_error):
            runner.run()
        return

    results = runner.run()[ResolveRemoteSourcePlugin.key]

    if expect_result:
        assert results['annotations']['remote_source_url']
        assert results['remote_source_json'] == REMOTE_SOURCE_JSON
        assert results['remote_source_path'] == expected_dowload_path(workflow)

        # A result means the plugin was enabled and executed successfully.
        # Let's verify the expected side effects.
        orchestrator_build_workspace = workflow.plugin_workspace[OrchestrateBuildPlugin.key]
        worker_params = orchestrator_build_workspace[WORKSPACE_KEY_OVERRIDE_KWARGS][None]
        assert worker_params['remote_source_url'] == CACHITO_REQUEST_DOWNLOAD_URL
        assert worker_params['remote_source_configs'] == CACHITO_REQUEST_CONFIG_URL
        expected = expected_build_args or CACHITO_BUILD_ARGS
        assert worker_params['remote_source_build_args'] == expected
        assert worker_params['remote_source_icm_url'] == CACHITO_ICM_URL

    return results
