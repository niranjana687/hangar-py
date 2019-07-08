import os
import logging
from collections import defaultdict
from contextlib import closing

import grpc

from . import merger
from . import constants as c
from .checkout import ReaderCheckout, WriterCheckout
from .context import Environments
from .diagnostics import graphing, ecosystem
from .records import heads, parsing, summarize, commiting, queries, hashs
from .remote.client import HangarClient
from .remote.content import ContentWriter, ContentReader
from .utils import is_valid_directory_path, is_suitable_user_key

logger = logging.getLogger(__name__)


class Repository(object):
    '''Launching point for all user operations in a Hangar repository.

    All interaction, including the ability to initialize a repo, checkout a
    commit (for either reading or writing), create a branch, merge branches, or
    generally view the contents or state of the local repository starts here.
    Just provide this class instance with a path to an existing Hangar
    repository, or to a directory one should be initialized, and all required
    data for starting your work on the repo will automatically be populated.

    Parameters
    ----------
    path : str
        local directory path where the Hangar repository exists (or initialized)
    '''

    def __init__(self, path):

        try:
            usr_path = is_valid_directory_path(path)
        except (TypeError, OSError, PermissionError) as e:
            logger.error(e, exc_info=False)
            raise

        repo_pth = os.path.join(usr_path, c.DIR_HANGAR)
        self._env: Environments = Environments(repo_path=repo_pth)
        self._repo_path: str = self._env.repo_path
        self._client: HangarClient = None

    def _repr_pretty_(self, p, cycle):
        '''provide a pretty-printed repr for ipython based user interaction.

        Parameters
        ----------
        p : printer
            io stream printer type object which is provided via ipython
        cycle : bool
            if the pretty-printer detects a cycle or infinite loop. Not a
            concern here since we just output the text and return, no looping
            required.

        '''
        res = f'Hangar {self.__class__.__name__}\
               \n    Repository Path  : {self._repo_path}\
               \n    Writer-Lock Free : {heads.writer_lock_held(self._env.branchenv)}\n'
        p.text(res)

    def __repr__(self):
        '''Override the default repr to show useful information to developers.

        Note: the pprint repr (ipython enabled) is seperately defined in
        :py:meth:`_repr_pretty_`. We specialize because we assume that anyone
        operating in a terminal-based interpreter is probably a more advanced
        developer-type, and expects traditional repr information instead of a
        user facing summary of the repo. Though if we're wrong, go ahead and
        feel free to reassign the attribute :) won't hurt our feelings, promise.

        Returns
        -------
        string
            formated representation of the object
        '''
        res = f'{self.__class__}(path={self._repo_path})'
        return res

    def __verify_repo_initialized(self):
        '''Internal method to verify repo inititilized before operations occur

        Raises
        ------
        RuntimeError
            If the repository db environments have not been initialized at the
            specified repo path.
        '''
        if not self._env.repo_is_initialized:
            msg = f'HANGAR RUNTIME ERROR:: Repository at path: {self._repo_path} has not '\
                  f'been initialized. Please run the `init_repo()` function'
            raise RuntimeError(msg)

    @property
    def repo_path(self):
        '''Return the path to the repository on disk, read-only attribute

        Returns
        -------
        str
            path to the specified repository, not including `__hangar` directory
        '''
        return os.path.dirname(self._repo_path)

    @property
    def writer_lock_held(self):
        '''Check if the writer lock is currently marked as held. Read-only attribute.

        Returns
        -------
        bool
            True is writer-lock is held, False if writer-lock is free.
        '''
        self.__verify_repo_initialized()
        return not heads.writer_lock_held(self._env.branchenv)

    def checkout(self, write=False, *, branch_name='master', commit=''):
        '''Checkout the repo at some point in time in either `read` or `write` mode.

        Only one writer instance can exist at a time. Write enabled checkout
        must must create a staging area from the HEAD commit of a branch. On the
        contrary, any number of reader checkouts can exist at the same time and
        can specify either a branch name or a commit hash.

        Parameters
        ----------
        write : bool, optional
            Specify if the checkout is write capable, defaults to False
        branch_name : str, optional
            name of the branch to checkout. This utilizes the state of the repo
            as it existed at the branch HEAD commit when this checkout object
            was instantiated, defaults to 'master'
        commit : str, optional
            specific hash of a commit to use for the checkout (instead of a
            branch HEAD commit). This argument takes precedent over a branch
            name parameter if it is set. Note: this only will be used in
            non-writeable checkouts, defaults to ''

        Raises
        ------
        ValueError
            If the value of `write` argument is not boolean

        Returns
        -------
        object
            Checkout object which can be used to interact with the repository
            data
        '''
        self.__verify_repo_initialized()
        try:
            if write is True:
                co = WriterCheckout(
                    repo_pth=self._repo_path,
                    branch_name=branch_name,
                    labelenv=self._env.labelenv,
                    hashenv=self._env.hashenv,
                    refenv=self._env.refenv,
                    stageenv=self._env.stageenv,
                    branchenv=self._env.branchenv,
                    stagehashenv=self._env.stagehashenv)
                return co
            elif write is False:
                commit_hash = self._env.checkout_commit(
                    branch_name=branch_name, commit=commit)

                co = ReaderCheckout(
                    base_path=self._repo_path,
                    labelenv=self._env.labelenv,
                    dataenv=self._env.cmtenv[commit_hash],
                    hashenv=self._env.hashenv,
                    branchenv=self._env.branchenv,
                    refenv=self._env.refenv,
                    commit=commit_hash)
                return co
            else:
                raise ValueError("Argument `write` only takes True or False as value")
        except (RuntimeError, ValueError) as e:
            logger.error(e, exc_info=False, extra=self._env.__dict__)
            raise e from None

    def clone(self, user_name: str, user_email: str, remote_address: str,
              *, remove_old: bool = False) -> str:
        '''Download a remote repository to the local disk.

        The clone method implemented here is very similar to a `git clone`
        operation. This method will pull all commit records, history, and data
        which are parents of the remote's `master` branch head commit. If a
        :class:`hangar.repository.Repository` exists at the specified directory,
        the operation will fail.

        Parameters
        ----------
        user_name : str
            Name of the person who will make commits to the repository. This
            information is recorded permanently in the commit records.
        user_email : str
            Email address of the repository user. This information is recorded
            permenantly in any commits created.
        remote_address : str
            location where the
            :class:`hangar.remote.server.HangarServer` process is
            running and accessable by the clone user.
        remove_old : bool, optional, kwarg only
            DANGER! DEVELOPMENT USE ONLY! If enabled, a
            :class:`hangar.repository.Repository` existing on disk at the same
            path as the requested clone location will be completly removed and
            replaced with the newly cloned repo. (the default is False, which
            will not modify any contents on disk and which will refuse to create
            a repository at a given location if one already exists there.)

        Returns
        -------
        str
            Name of the master branch for the newly cloned repository.
        '''
        self.init(user_name=user_name, user_email=user_email, remove_old=remove_old)
        self.add_remote(remote_name='origin', remote_address=remote_address)
        branch_name = self.fetch(remote_name='origin', branch_name='master', concat_branch_names=False)
        co = self.checkout(write=True, branch_name='master')
        co.reset_staging_area()
        co.close()
        return branch_name

    def fetch_data(self, remote_name: str, commit_hash: str) -> str:
        '''Partial clone fetch data operation.

        Parameters
        ----------
        remote_name : str
            name of the remote server
        commit_hash : str
            commit hash to retrieve data for

        Returns
        -------
        str
            commit hash of the data which was returned.
        '''
        self.__verify_repo_initialized()
        address = heads.get_remote_address(branchenv=self._env.branchenv, name=remote_name)
        self._client = HangarClient(envs=self._env, address=address)
        CW = ContentWriter(self._env)

        with closing(self._client) as client:
            client: HangarClient  # type hint

            # TODO: Should not have to get all data hashs
            commit_hash = self._env.checkout_commit(commit=commit_hash)
            cmtData_hashs = set(queries.RecordQuery(self._env.cmtenv[commit_hash]).data_hashes())
            hashQuery = hashs.HashQuery(self._env.hashenv)
            hashMap = hashQuery.map_all_hash_keys_raw_to_values_raw()
            m_schema_hash_map = defaultdict(list)
            for digest in cmtData_hashs:
                hashSpec = hashMap[digest]
                if hashSpec.backend == '50':
                    m_schema_hash_map[hashSpec.schema_hash].append(digest)

            for schema in list(m_schema_hash_map.keys()):
                hashes = m_schema_hash_map[schema]
                while len(hashes) > 0:
                    ret = client.fetch_data(schema, hashes)
                    saved_digests = CW.data(schema, ret)
                    hashes = list(set(hashes).difference(set(saved_digests)))

            commiting.move_process_data_to_store(self._repo_path, remote_operation=True)

        return commit_hash

    def fetch(self, remote_name: str, branch_name: str,
              *, concat_branch_names: bool = True) -> str:
        '''Retrieve new commits made on a remote repository branch.

        This is symantecally identical to a `git fetch` command. Any new commits
        along the branch will be retrived, but placed on an isolated branch to
        the local copy (ie. ``remote_name/branch_name``). In order to unify
        histories, simply merge the remote branch into the local branch.

        Parameters
        ----------
        remote_name : str
            name of the remote repository to fetch from (ie. ``origin``)
        branch_name : str
            name of the branch to fetch the commit references for.
        concat_branch_names : bool, optional, kwarg only
            DEVELOPER USE ONLY! TODO: remove this...

        Returns
        -------
        str
            Name of the branch which stores the retrieved commits.
        '''
        self.__verify_repo_initialized()
        address = heads.get_remote_address(branchenv=self._env.branchenv, name=remote_name)
        self._client = HangarClient(envs=self._env, address=address)
        CW = ContentWriter(self._env)

        with closing(self._client) as client:
            client: HangarClient  # type hinting for development
            try:
                c_bcommit = heads.get_branch_head_commit(self._env.branchenv, branch_name)
                c_bhistory = summarize.list_history(
                    self._env.refenv, self._env.branchenv, branch_name=branch_name)
                s_branch = client.fetch_branch_record(branch_name)
                if s_branch.error.code == 0:
                    s_bcommit = s_branch.rec.commit
                    if s_bcommit == c_bcommit:
                        logger.warning(f'NoOp: serv HEAD {s_bcommit} == client HEAD {c_bcommit}')
                        return
                    elif s_bcommit in c_bhistory['order']:
                        logger.warning(f'REJECTED: server HEAD {s_bcommit} in client history')
                        return
            except ValueError:
                s_branch = client.fetch_branch_record(branch_name)

            res = client.fetch_find_missing_commits(branch_name)
            m_commits = res.commits
            m_labels = set()
            for commit in m_commits:
                m_labels.update(client.fetch_find_missing_labels(commit))
                schema_res = client.fetch_find_missing_schemas(commit)
                for schema in schema_res.schema_digests:
                    schema_hash, schemaVal = client.fetch_schema(schema)
                    CW.schema(schema_hash, schemaVal)

                m_hashes = client.fetch_find_missing_hash_records(commit)
                m_schema_hash_map = defaultdict(list)
                for digest, schema_hash in m_hashes:
                    m_schema_hash_map[schema_hash].append((digest, schema_hash))
                for schema_hash, recieved_data in m_schema_hash_map.items():
                    CW.data(schema_hash, recieved_data, backend='50')

            for label in m_labels:
                recieved_hash, labelVal = client.fetch_label(label)
                CW.label(recieved_hash, labelVal)
            for commit in m_commits:
                cmt, parentVal, specVal, refVal = client.fetch_commit_record(commit)
                CW.commit(cmt, parentVal, specVal, refVal)

            bHEAD = s_branch.rec.commit
            bName = f'{remote_name}/{branch_name}' if concat_branch_names else branch_name
            try:
                heads.create_branch(
                    self._env.branchenv, branch_name=bName, base_commit=bHEAD)
            except ValueError:
                heads.set_branch_head_commit(
                    self._env.branchenv, branch_name=bName, commit_hash=bHEAD)
            return bName

    def push(self, remote_name: str, branch_name: str, *,
             username: str = '', password: str = '') -> bool:
        '''push changes made on a local repository to a remote repository.

        This method is symantically identical to a ``git push`` operation.
        Any local updates will be sent to the remote repository.

        .. note::

            The current implementation is not capable of performing a
            ``force push`` operation. As such, remote branches with diverged
            histories to the local repo must be retrieved, locally merged,
            then re-pushed. This feature will be added in the near future.

        Parameters
        ----------
        remote_name : str
            name of the remote repository to make the push on.
        branch_name : str
            Name of the branch to push to the remote. If the branch name does
            not exist on the remote, the it will be created
        auth_username : str, optional, kwarg-only
            credentials to use for authentication if repository push restrictions
            are enabled, by default ''.
        auth_password : str, optional, kwarg-only
            credentials to use for authentication if repository push restrictions
            are enabled, by default ''.

        Returns
        -------
        bool
            True if the operation succeeded, Otherwise False
        '''
        self.__verify_repo_initialized()
        address = heads.get_remote_address(branchenv=self._env.branchenv, name=remote_name)
        self._client = HangarClient(
            envs=self._env, address=address, auth_username=username, auth_password=password)
        CR = ContentReader(self._env)

        with closing(self._client) as client:
            client: HangarClient  # type hinting for development

            c_bcommit = heads.get_branch_head_commit(self._env.branchenv, branch_name)
            c_bhistory = summarize.list_history(
                refenv=self._env.refenv, branchenv=self._env.branchenv, branch_name=branch_name)
            s_branch = client.fetch_branch_record(branch_name)
            if s_branch.error.code == 0:
                s_bcommit = s_branch.rec.commit
                if s_bcommit == c_bcommit:
                    logger.warning(f'NoOp: serv HEAD {s_bcommit} == client HEAD {c_bcommit}')
                    return False
                elif (s_bcommit not in c_bhistory['order']) and (s_bcommit != ''):
                    logger.warning(f'REJECTED: server branch has commits not on client')
                    return False

            try:
                res = client.push_find_missing_commits(branch_name)
            except grpc.RpcError as rpc_error:
                if rpc_error.code() == grpc.StatusCode.PERMISSION_DENIED:
                    raise PermissionError(f'{rpc_error.code()}: {rpc_error.details()}')
                else:
                    raise rpc_error

            m_labels, m_commits = set(), res.commits
            for commit in m_commits:
                schema_res = client.push_find_missing_schemas(commit)
                for schema in schema_res.schema_digests:
                    schemaVal = CR.schema(schema)
                    if not schemaVal:
                        raise KeyError(f'no schema with hash: {schema} exists')
                    client.push_schema(schema, schemaVal)

                mis_hashes_sch = client.push_find_missing_hash_records(commit)
                missing_schema_hashs = defaultdict(list)
                for hsh, schema in mis_hashes_sch.items():
                    missing_schema_hashs[schema].append(hsh)
                for schema, hashes in missing_schema_hashs.items():
                    client.push_data(schema, hashes)
                missing_labels = client.push_find_missing_labels(commit)
                m_labels.update(missing_labels)

            for label in m_labels:
                labelVal = CR.label(label)
                if not labelVal:
                    raise KeyError(f'no label with hash: {label} exists')
                client.push_label(label, labelVal)

            for commit in m_commits:
                cmtContent = CR.commit(commit)
                if not cmtContent:
                    raise KeyError(f'no commit with hash: {commit} exists')
                client.push_commit_record(commit=cmtContent.commit,
                                          parentVal=cmtContent.cmtParentVal,
                                          specVal=cmtContent.cmtSpecVal,
                                          refVal=cmtContent.cmtRefVal)

            branchHead = heads.get_branch_head_commit(self._env.branchenv, branch_name)
            client.push_branch_record(branch_name, branchHead)
            return True

    def _ping_server(self, remote_name: str, *, username: str = '', password: str = '') -> str:
        '''ping the remote server with provided name.

        Parameters
        ----------
        remote_name : str
            name of the remote repository to make the push on.
        auth_username : str, optional, kwarg-only
            credentials to use for authentication if repository push restrictions
            are enabled, by default ''.
        auth_password : str, optional, kwarg-only
            credentials to use for authentication if repository push restrictions
            are enabled, by default ''.

        Returns
        -------
        string
            if success, should result in "PONG"
        '''
        self.__verify_repo_initialized()
        address = heads.get_remote_address(branchenv=self._env.branchenv, name=remote_name)
        self._client = HangarClient(
            envs=self._env, address=address, auth_username=username, auth_password=password)
        with closing(self._client) as client:
            res = client.ping_pong()
            return res

    def add_remote(self, remote_name: str, remote_address: str) -> bool:
        '''Add a remote to the repository accessible by `name` at `address`.

        Parameters
        ----------
        remote_name : str
            the name which should be used to refer to the remote server (ie:
            'origin')
        remote_address : str
            the IP:PORT where the hangar server is running

        Returns
        -------
        str
            The name of the remote added to the client's server list.

        Raises
        ------
        ValueError
            If a remote with the provided name is already listed on this client,
            No-Op. In order to update a remote server address, it must be
            removed and then re-added with the desired address.
        '''
        self.__verify_repo_initialized()
        succ = heads.add_remote(
            branchenv=self._env.branchenv,
            name=remote_name,
            address=remote_address)

        if succ is False:
            raise ValueError(
                f'Remote with name: {remote_name} has been previously added.'
                f'No operation (update to the server channel address) was saved.')
        return remote_name

    def remove_remote(self, remote_name: str) -> str:
        '''Remove a remote repository from the branch records

        Parameters
        ----------
        remote_name : str
            name of the remote to remove the reference to

        Raises
        ------
        ValueError
            If a remote with the provided name does not exist

        Returns
        -------
        str
            The channel address which was removed at the given remote name
        '''
        self.__verify_repo_initialized()
        try:
            rm_address = heads.remove_remote(
                branchenv=self._env.branchenv, name=remote_name)
        except KeyError:
            err = f'No remote reference with name: {remote_name}'
            raise ValueError(err)

        return rm_address

    def list_remotes(self):
        '''List names of all remotes recorded in the repo

        Returns
        -------
        list
            list of str containing all remotes listed in the repo.
        '''
        self.__verify_repo_initialized()
        remotes = heads.get_remote_names(self._env.branchenv)
        return remotes

    def init(self, user_name, user_email, remove_old=False):
        '''Initialize a Hangar repositor at the specified directory path.

        This function must be called before a checkout can be performed.

        Parameters
        ----------
        user_name : str
            Name of the repository user.
        user_email : str
            Email address of the respository user.
        remove_old : bool, optional
            DEVELOPER USE ONLY -- remove and reinitialize a Hangar
            repository at the given path, defaults to False

        Returns
        -------
        str
            the full directory path where the Hangar repository was
            initialized on disk.
        '''
        pth = self._env._init_repo(
            user_name=user_name, user_email=user_email, remove_old=remove_old)
        return pth

    def log(self, branch_name=None, commit_hash=None,
            *, return_contents=False, show_time=False, show_user=False):
        '''Displays a pretty printed commit log graph to the terminal.

        .. note::

            For programatic access, the return_contents value can be set to true
            which will retrieve relevant commit specifications as dictionary
            elements.

        Parameters
        ----------
        branch_name : str
            The name of the branch to start the log process from. (Default value
            = None)
        commit_hash : str
            The commit hash to start the log process from. (Default value = None)
        return_contents : bool, optional, kwarg only
            If true, return the commit graph specifications in a dictionary
            suitable for programatic access/evalutation.
        show_time : bool, optional, kwarg only
            If true and return_contents is False, show the time of each commit
            on the printed log graph
        show_user : bool, optional, kwarg only
            If true and return_contents is False, show the committer of each
            commit on the printed log graph
        Returns
        -------
        dict
            Dict containing the commit ancestor graph, and all specifications.
        '''
        self.__verify_repo_initialized()
        res = summarize.list_history(
            refenv=self._env.refenv,
            branchenv=self._env.branchenv,
            branch_name=branch_name,
            commit_hash=commit_hash)

        if return_contents:
            return res
        else:
            branchMap = heads.commit_hash_to_branch_name_map(branchenv=self._env.branchenv)
            g = graphing.Graph()
            g.show_nodes(
                dag=res['ancestors'],
                spec=res['specs'],
                branch=branchMap,
                start=res['head'],
                order=res['order'],
                show_time=show_time,
                show_user=show_user)

    def summary(self, *, branch_name='', commit='', return_contents=False):
        '''Print a summary of the repository contents to the terminal

        .. note::

            Programatic access is provided by the return_contents argument.

        Parameters
        ----------
        branch_name : str
            A specific branch name whose head commit will be used as the summary
            point (Default value = '')
        commit : str
            A specific commit hash which should be used as the summary point.
            (Default value = '')
        return_contents : bool
            If true, return a full log of what records are in the repository at
            the summary point. (Default value = False)

        Returns
        -------
        dict
            contents of the entire repository (if `return_contents=True`)
        '''
        self.__verify_repo_initialized()
        ppbuf, res = summarize.summary(self._env, branch_name=branch_name, commit=commit)
        if return_contents is True:
            return res
        else:
            print(ppbuf.getvalue())

    def _details(self):
        '''DEVELOPER USE ONLY: Dump some details about the underlying db structure to disk.
        '''
        print(summarize.details(self._env.branchenv).getvalue())
        print(summarize.details(self._env.refenv).getvalue())
        print(summarize.details(self._env.hashenv).getvalue())
        print(summarize.details(self._env.labelenv).getvalue())
        print(summarize.details(self._env.stageenv).getvalue())
        print(summarize.details(self._env.stagehashenv).getvalue())
        for commit, commitenv in self._env.cmtenv.items():
            print(summarize.details(commitenv).getvalue())
        return

    def _ecosystem_details(self):
        '''DEVELOPER USER ONLY: log and return package versions on the sytem.
        '''
        eco = ecosystem.get_versions()
        return eco

    def merge(self, message, master_branch, dev_branch):
        '''Perform a merge of the changes made on two branches.

        Parameters
        ----------
        message: str
            Commit message to use for this merge.
        master_branch : str
            name of the master branch to merge into
        dev_branch : str
            name of the dev/feature branch to merge

        Returns
        -------
        str
            Hashof the commit which is written if possible.
        '''
        self.__verify_repo_initialized()
        commit_hash = merger.select_merge_algorithm(
            message=message,
            branchenv=self._env.branchenv,
            stageenv=self._env.stageenv,
            refenv=self._env.refenv,
            stagehashenv=self._env.stagehashenv,
            master_branch_name=master_branch,
            dev_branch_name=dev_branch,
            repo_path=self._repo_path)

        return commit_hash

    def create_branch(self, branch_name, base_commit=None):
        '''create a branch with the provided name from a certain commit.

        If no base commit hash is specified, the current writer branch HEAD
        commit is used as the base_commit hash for the branch. Note that
        creating a branch does not actually create a checkout object for
        interaction with the data. to interact you must use the repository
        checkout method to properly initialize a read (or write) enabled
        checkout object.

        Parameters
        ----------
        branch_name : str
            name to assign to the new branch
        base_commit : str, optional
            commit hash to start the branch root at. if not specified, the
            writer branch HEAD commit at the time of execution will be used,
            defaults to None

        Returns
        -------
        str
            name of the branch which was created
        '''
        self.__verify_repo_initialized()
        if not is_suitable_user_key(branch_name):
            msg = f'HANGAR VALUE ERROR:: branch name provided: `{branch_name}` invalid. '\
                  f'Must only contain alpha-numeric or "." "_" "-" ascii characters.'
            e = ValueError(msg)
            logger.error(e, exc_info=False)
            raise e
        didCreateBranch = heads.create_branch(
            branchenv=self._env.branchenv,
            branch_name=branch_name,
            base_commit=base_commit)
        return didCreateBranch

    def remove_branch(self, branch_name):
        '''Not Implemented
        '''
        raise NotImplementedError()

    def list_branches(self):
        '''list all branch names created in the repository.

        Returns
        -------
        list of str
            the branch names recorded in the repository
        '''
        self.__verify_repo_initialized()
        branches = heads.get_branch_names(self._env.branchenv)
        return branches

    def force_release_writer_lock(self):
        '''Force release the lock left behind by an unclosed writer-checkout

        .. warning::

            *NEVER USE THIS METHOD IF WRITER PROCESS IS CURRENTLY ACTIVE.* At the time
            of writing, the implications of improper/malicious use of this are not
            understood, and there is a a risk of of undefined behavior or (potentially)
            data corruption.

            At the moment, the responsibility to close a write-enabled checkout is
            placed entirely on the user. If the `close()` method is not called
            before the program terminates, a new checkout with write=True will fail.
            The lock can only be released via a call to this method.

        .. note::

            This entire mechanism is subject to review/replacement in the future.

        Returns
        -------
        bool
            if the operation was successful.
        '''
        self.__verify_repo_initialized()
        forceReleaseSentinal = parsing.repo_writer_lock_force_release_sentinal()
        success = heads.release_writer_lock(self._env.branchenv, forceReleaseSentinal)
        return success
