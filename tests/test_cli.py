from os import getcwd

import numpy as np
import pytest
from click.testing import CliRunner

from hangar import Repository
from hangar.cli import cli
from conftest import backend_params


# -------------------------------- test data ----------------------------------


help_res = 'Usage: main [OPTIONS] COMMAND [ARGS]...\n'\
           '\n'\
           'Options:\n'\
           '  --version  display current Hangar Version\n'\
           '  --help     Show this message and exit.\n'\
           '\n'\
           'Commands:\n'\
           '  branch      operate on and list branch pointers.\n'\
           '  clone       Initialize a repository at the current path and fetch updated...\n'\
           '  export      export ARRAYSET sample data as it existed a STARTPOINT to some...\n'\
           '  fetch       Retrieve the commit history from REMOTE for BRANCH.\n'\
           '  fetch-data  Get data from REMOTE referenced by STARTPOINT (short-commit or...\n'\
           '  import      Import file(s) at PATH to ARRAYSET in the staging area.\n'\
           '  init        Initialize an empty repository at the current path\n'\
           '  log         Display commit graph starting at STARTPOINT (short-digest or...\n'\
           '  push        Upload local BRANCH commit history / data to REMOTE server.\n'\
           '  remote      Operations for working with remote server references\n'\
           '  server      Start a hangar server, initializing one if does not exist.\n'\
           '  summary     Display content summary at STARTPOINT (short-digest or branch).\n'\
           '  view        Use a plugin to view the data of some SAMPLE in ARRAYSET at...\n'


# ------------------------------- begin tests ---------------------------------


def test_help():
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ['--help'])
        assert res.exit_code == 0
        assert res.stdout == help_res


def test_version_long_option():
    import hangar
    runner = CliRunner()
    with runner.isolated_filesystem():
        res = runner.invoke(cli.main, ['--version'])
        assert res.exit_code == 0
        assert res.stdout == f'main, version {hangar.__version__}\n'


def test_init_repo():
    runner = CliRunner()
    with runner.isolated_filesystem():
        P = getcwd()
        repo = Repository(P, exists=False)
        res = runner.invoke(cli.init, ['--name', 'test', '--email', 'test@foo.com'], obj=repo)
        assert res.exit_code == 0
        assert repo._Repository__verify_repo_initialized() is None


def test_clone(written_two_cmt_server_repo):
    server, base_repo = written_two_cmt_server_repo
    runner = CliRunner()
    with runner.isolated_filesystem():
        P = getcwd()
        new_repo = Repository(P, exists=False)
        res = runner.invoke(
            cli.clone,
            ['--name', 'Foo Tester', '--email', 'foo@email.com', f'{server}'], obj=new_repo)

        assert res.exit_code == 0

        newLog = new_repo.log(return_contents=True)
        baseLog = base_repo.log(return_contents=True)
        assert newLog == baseLog
        assert new_repo.summary() == base_repo.summary()


@pytest.mark.parametrize('backend', backend_params)
def test_push_fetch_records(server_instance, backend):

    runner = CliRunner()
    with runner.isolated_filesystem():
        repo = Repository(getcwd(), exists=False)
        repo.init('foo', 'bar')
        dummyData = np.arange(50)
        co1 = repo.checkout(write=True, branch='master')
        co1.arraysets.init_arrayset(
            name='dummy', prototype=dummyData, named_samples=True, backend=backend)
        for idx in range(10):
            dummyData[:] = idx
            co1.arraysets['dummy'][str(idx)] = dummyData
        co1.metadata['hello'] = 'world'
        co1.metadata['somemetadatakey'] = 'somemetadatavalue'
        cmt1 = co1.commit('first commit adding dummy data and hello meta')
        co1.close()

        repo.create_branch('testbranch')
        co2 = repo.checkout(write=True, branch='testbranch')
        for idx in range(10, 20):
            dummyData[:] = idx
            co2.arraysets['dummy'][str(idx)] = dummyData
        co2.metadata['foo'] = 'bar'
        cmt2 = co2.commit('first commit on test branch adding non-conflict data and meta')
        co2.close()

        repo.remote.add('origin', server_instance)

        res = runner.invoke(cli.push, ['origin', 'master'], obj=repo)
        assert res.exit_code == 0
        res = runner.invoke(cli.push, ['origin', 'testbranch'], obj=repo)
        assert res.exit_code == 0



@pytest.mark.parametrize('backend', backend_params)
@pytest.mark.parametrize('options', [
    ['origin', 'testbranch'],
    ['origin', 'master'],
    ['origin', 'testbranch', '--all-history'],
    ['origin', 'master', '--all-history'],
    ['origin', 'testbranch', '--aset', 'data'],
    ['origin', 'master', '--aset', 'data'],
    ['origin', 'testbranch', '--aset', 'data', '--all-history'],
    ['origin', 'master', '--aset', 'data', '--all-history'],
    ['origin', 'testbranch', '--aset', 'data', '--all-history'],
    ['origin', 'master', '--nbytes', '3Kb'],
])
def test_fetch_records_and_data(server_instance, backend, options):

    runner = CliRunner()
    with runner.isolated_filesystem():
        repo = Repository(getcwd(), exists=False)
        repo.init('foo', 'bar')
        dummyData = np.arange(50)
        co1 = repo.checkout(write=True, branch='master')
        co1.arraysets.init_arrayset(
            name='dummy', prototype=dummyData, named_samples=True, backend=backend)
        for idx in range(10):
            dummyData[:] = idx
            co1.arraysets['dummy'][str(idx)] = dummyData
        co1.metadata['hello'] = 'world'
        co1.metadata['somemetadatakey'] = 'somemetadatavalue'
        cmt1 = co1.commit('first commit adding dummy data and hello meta')
        co1.close()

        repo.create_branch('testbranch')
        co2 = repo.checkout(write=True, branch='testbranch')
        for idx in range(10, 20):
            dummyData[:] = idx
            co2.arraysets['dummy'][str(idx)] = dummyData
        co2.metadata['foo'] = 'bar'
        cmt2 = co2.commit('first commit on test branch adding non-conflict data and meta')
        co2.close()

        repo.remote.add('origin', server_instance)

        res = runner.invoke(cli.push, ['origin', 'master'], obj=repo)
        assert res.exit_code == 0
        res = runner.invoke(cli.push, ['origin', 'testbranch'], obj=repo)
        assert res.exit_code == 0

    with runner.isolated_filesystem():
        repo = Repository(getcwd(), exists=False)
        res = runner.invoke(
            cli.clone,
            ['--name', 'Foo Tester', '--email', 'foo@email.com', f'{server_instance}'], obj=repo)
        assert res.exit_code == 0

        res = runner.invoke(cli.fetch_records, ['origin', 'testbranch'], obj=repo)
        assert res.exit_code == 0
        res = runner.invoke(cli.branch_create, ['testbranch', 'origin/testbranch'], obj=repo)
        assert res.exit_code == 0
        res = runner.invoke(cli.fetch_data, options, obj=repo)
        assert res.exit_code == 0


def test_add_remote():
    from hangar.remotes import RemoteInfo

    runner = CliRunner()
    with runner.isolated_filesystem():
        P = getcwd()
        repo = Repository(P, exists=False)
        res = runner.invoke(cli.init, ['--name', 'test', '--email', 'test@foo.com'], obj=repo)
        assert res.exit_code == 0

        res = runner.invoke(cli.add_remote, ['origin', 'localhost:50051'], obj=repo)
        assert res.exit_code == 0
        assert res.stdout == "RemoteInfo(name='origin', address='localhost:50051')\n"

        remote_list = repo.remote.list_all()
        assert remote_list == [RemoteInfo(name='origin', address='localhost:50051')]


def test_remove_remote():
    from hangar.remotes import RemoteInfo

    runner = CliRunner()
    with runner.isolated_filesystem():
        P = getcwd()
        repo = Repository(P, exists=False)
        res = runner.invoke(cli.init, ['--name', 'test', '--email', 'test@foo.com'], obj=repo)
        assert res.exit_code == 0

        res = runner.invoke(cli.add_remote, ['origin', 'localhost:50051'], obj=repo)
        assert res.exit_code == 0
        assert res.stdout == "RemoteInfo(name='origin', address='localhost:50051')\n"

        remote_list = repo.remote.list_all()
        assert remote_list == [RemoteInfo(name='origin', address='localhost:50051')]

        res = runner.invoke(cli.remove_remote, ['origin'], obj=repo)
        assert res.exit_code == 0
        assert res.stdout == "RemoteInfo(name='origin', address='localhost:50051')\n"
        assert repo.remote.list_all() == []


def test_list_all_remotes(managed_tmpdir):
    from hangar.remotes import RemoteInfo

    runner = CliRunner()
    with runner.isolated_filesystem():
        P = getcwd()
        repo = Repository(P, exists=False)
        res = runner.invoke(cli.init, ['--name', 'test', '--email', 'test@foo.com'], obj=repo)
        assert res.exit_code == 0

        res = runner.invoke(cli.add_remote, ['origin', 'localhost:50051'], obj=repo)
        assert res.exit_code == 0
        assert res.stdout == "RemoteInfo(name='origin', address='localhost:50051')\n"
        res = runner.invoke(cli.add_remote, ['upstream', 'foo:ip'], obj=repo)
        assert res.exit_code == 0
        assert res.stdout == "RemoteInfo(name='upstream', address='foo:ip')\n"

        remote_list = repo.remote.list_all()
        assert remote_list == [
            RemoteInfo(name='origin', address='localhost:50051'),
            RemoteInfo(name='upstream', address='foo:ip')
        ]

        res = runner.invoke(cli.list_remotes, obj=repo)
        assert res.exit_code == 0
        expected_stdout = "[RemoteInfo(name='origin', address='localhost:50051'), "\
                          "RemoteInfo(name='upstream', address='foo:ip')]\n"
        assert res.stdout == expected_stdout


def test_summary(written_two_cmt_server_repo, capsys):
    server, base_repo = written_two_cmt_server_repo
    runner = CliRunner()
    with runner.isolated_filesystem():
        with capsys.disabled():
            P = getcwd()
            new_repo = Repository(P, exists=False)
            res = runner.invoke(
                cli.clone,
                ['--name', 'Foo Tester', '--email', 'foo@email.com', f'{server}'], obj=new_repo)

            assert res.exit_code == 0
            assert new_repo.summary() == base_repo.summary()

        new_repo.summary()

        with capsys.disabled():
            res = runner.invoke(cli.summary, obj=new_repo)
            assert res.stdout == f"{capsys.readouterr().out}\n"


def test_log(written_two_cmt_server_repo, capsys):
    server, base_repo = written_two_cmt_server_repo
    runner = CliRunner()
    with runner.isolated_filesystem():
        with capsys.disabled():
            P = getcwd()
            new_repo = Repository(P, exists=False)
            res = runner.invoke(
                cli.clone,
                ['--name', 'Foo Tester', '--email', 'foo@email.com', f'{server}'], obj=new_repo)

            assert res.exit_code == 0
            assert new_repo.log() == base_repo.log()

        new_repo.log()

        with capsys.disabled():
            res = runner.invoke(cli.log, ['master'], obj=new_repo)
            assert res.stdout == f"{capsys.readouterr().out}\n"


def test_branch_create_and_list(written_two_cmt_server_repo):
    server, base_repo = written_two_cmt_server_repo

    co = base_repo.checkout(write=True)
    cmt = co.commit_hash
    co.close()

    runner = CliRunner()
    with runner.isolated_filesystem():
        P = getcwd()
        new_repo = Repository(P, exists=False)
        res = runner.invoke(
            cli.clone,
            ['--name', 'Foo Tester', '--email', 'foo@email.com', f'{server}'], obj=new_repo)
        assert res.exit_code == 0

        res = runner.invoke(cli.branch_create, ['testbranch'], obj=new_repo)
        assert res.exit_code == 0
        assert res.stdout == f"Created BRANCH: testbranch HEAD: {cmt}\n"

        branches = new_repo.list_branches()
        assert branches == ['master', 'origin/master', 'testbranch']

        res = runner.invoke(cli.branch_list, obj=new_repo)
        assert res.exit_code == 0
        assert res.stdout == "['master', 'origin/master', 'testbranch']\n"


@pytest.mark.filterwarnings("ignore:Arrayset.* contains `reference-only` samples")
def test_branch_create_and_delete(written_two_cmt_server_repo):
    server, base_repo = written_two_cmt_server_repo

    co = base_repo.checkout(write=True)
    cmt = co.commit_hash
    co.close()

    runner = CliRunner()
    with runner.isolated_filesystem():
        P = getcwd()
        new_repo = Repository(P, exists=False)
        res = runner.invoke(
            cli.clone,
            ['--name', 'Foo Tester', '--email', 'foo@email.com', f'{server}'], obj=new_repo)
        assert res.exit_code == 0

        res = runner.invoke(cli.branch_create, ['testbranch'], obj=new_repo)
        assert res.exit_code == 0
        assert res.stdout == f"Created BRANCH: testbranch HEAD: {cmt}\n"

        branches = new_repo.list_branches()
        assert branches == ['master', 'origin/master', 'testbranch']

        res = runner.invoke(cli.branch_remove, ['testbranch'], obj=new_repo)
        assert res.exit_code == 0
        assert res.stdout == f"Deleted BRANCH: testbranch HEAD: {cmt}\n"

        branches = new_repo.list_branches()
        assert branches == ['master', 'origin/master']

        new_repo.create_branch('secondtest')
        co = new_repo.checkout(write=True, branch='secondtest')
        co.metadata['foodadaa'] = '34a345'
        newDigest = co.commit('dummy commit')
        co.close()

        # re-open with staging set to master so we can try to delete secondtest
        co = new_repo.checkout(write=True, branch='master')
        co.close()

        res = runner.invoke(cli.branch_remove, ['secondtest'], obj=new_repo)
        assert res.exit_code == 1

        res = runner.invoke(cli.branch_remove, ['secondtest', '-f'], obj=new_repo)
        assert res.exit_code == 0
        assert res.stdout == f"Deleted BRANCH: secondtest HEAD: {newDigest}\n"

        res = runner.invoke(cli.branch_list, obj=new_repo)
        assert res.exit_code == 0
        assert res.stdout == "['master', 'origin/master']\n"


def test_start_server(managed_tmpdir):
    import time
    runner = CliRunner()
    with runner.isolated_filesystem():
        startTime = time.time()
        res = runner.invoke(cli.server, ['--ip', 'localhost', '--port', '50111', '--timeout', '1'])
        assert time.time() - startTime <= 1.8  # buffer to give it time to stop
        assert res.exit_code == 0
        assert 'Hangar Server Started' in res.stdout