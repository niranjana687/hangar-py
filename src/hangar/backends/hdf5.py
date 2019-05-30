import logging
import math
import os
from os.path import splitext as psplitext
import re
import subprocess
from collections import namedtuple, ChainMap
from os.path import join as pjoin
from functools import partial

import h5py
import numpy as np

from .. import __version__, config
from ..utils import find_next_prime, symlink_rel, random_string

logger = logging.getLogger(__name__)

SEP = config.get('hangar.seps.key')
LISTSEP = config.get('hangar.seps.list')
SLICESEP = config.get('hangar.seps.slice')
HASHSEP = config.get('hangar.seps.hash')

STAGE_DATA_DIR = config.get('hangar.repository.stage_data_dir')
REMOTE_DATA_DIR = config.get('hangar.repository.remote_data_dir')
DATA_DIR = config.get('hangar.repository.data_dir')
STORE_DATA_DIR = config.get('hangar.repository.store_data_dir')

HDF_CHUNK_OPTS = config.get('hangar.hdf5.dataset.chunking')
HDF_DSET_CONTENTS = config.get('hangar.hdf5.contents')
HDF_ATTRS = config.get('hangar.hdf5.attributes.keys')

HDF_DSET_FILTERS = config.get('hangar.hdf5.dataset.filters.default')
if HDF_DSET_FILTERS['complib'].startswith('blosc'):
    bloscFilterAvail = h5py.h5z.filter_avail(32001)
    if not bloscFilterAvail:
        HDF_DSET_FILTERS = config.get('hangar.hdf5.dataset.filters.backup')


class HDF5_00_Parser(object):

    __slots__ = ['FmtBackend', 'FmtCode', 'FmtCodeIdx', 'ShapeFmtRE', 'DataHashSpec']

    def __init__(self):

        self.FmtBackend = 'hdf5_00'
        self.FmtCode = '00'
        self.FmtCodeIdx = 3

        # match and remove the following characters: '['   ']'   '('   ')'   ','
        self.ShapeFmtRE = re.compile('[,\(\)\[\]]')
        self.DataHashSpec = namedtuple(
            typename='DataHashSpec',
            field_names=['backend', 'uid', 'dataset', 'dataset_idx', 'shape'])

    def encode(self, uid, dataset, dataset_idx, shape) -> bytes:
        '''converts the hdf5 data has spec to an appropriate db value

        Parameters
        ----------
        uid : str
            the file name prefix which the data is written to.
        dataset : str
            collection (ie. hdf5 dataset) name to find find this data piece.
        dataset_idx : int or str
            collection first axis index in which this data piece resides.
        shape : tuple
            shape of the data sample written to the collection idx. ie:
            what subslices of the hdf5 dataset should be read to retrieve
            the sample as recorded.

        Returns
        -------
        bytes
            hash data db value recording all input specifications.
        '''
        out_str = f'{self.FmtCode}{SEP}{uid}'\
                  f'{HASHSEP}'\
                  f'{dataset}{LISTSEP}{dataset_idx}'\
                  f'{SLICESEP}'\
                  f'{self.ShapeFmtRE.sub("", str(shape))}'
        return out_str.encode()

    def decode(self, db_val: bytes) -> namedtuple:
        '''converts an hdf5 data hash db val into an hdf5 data python spec.

        Parameters
        ----------
        db_val : bytestring
            data hash db value

        Returns
        -------
        namedtuple
            hdf5 data hash specification containing `backend`, `schema`,
            `instance`, `dataset`, `dataset_idx`, `shape`
        '''
        db_str = db_val.decode()[self.FmtCodeIdx:]

        uid, _, dset_vals = db_str.partition(HASHSEP)

        dataset_vs, _, shape_vs = dset_vals.rpartition(SLICESEP)
        dataset, dataset_idx = dataset_vs.split(LISTSEP)
        # if the data is of empty shape -> ()
        shape = () if shape_vs == '' else tuple([int(x) for x in shape_vs.split(LISTSEP)])

        raw_val = self.DataHashSpec(backend=self.FmtBackend,
                                    uid=uid,
                                    dataset=dataset,
                                    dataset_idx=dataset_idx,
                                    shape=shape)
        return raw_val


'''
Dense Array Methods
-------------------
'''


class HDF5_00_FileHandles(object):
    '''Singleton to manage HDF5 file handles.

    When in SWMR-write mode, no more than a single file handle can be in the
    "writeable" state. This is an issue where multiple datasets may need to
    write to the same dataset schema.
    '''

    def __init__(self, repo_path: os.PathLike, schema_shape: tuple, schema_dtype: np.dtype):
        self.repo_path = repo_path
        self.schema_shape = schema_shape
        self.schema_dtype = schema_dtype

        self.rFp = {}
        self.wFp = {}
        self.Fp = ChainMap(self.rFp, self.wFp)

        self.mode: str = None
        self.hIdx: int = None
        self.w_uid: str = None
        self.hMaxSize: int = None
        self.hNextPath: int = None
        self.hColsRemain: int = None

        self.slcExpr = np.s_
        self.slcExpr.maketuple = False
        self.fmtParser = HDF5_00_Parser()

        self.STAGEDIR = pjoin(self.repo_path, STAGE_DATA_DIR, self.fmtParser.FmtCode)
        self.REMOTEDIR = pjoin(self.repo_path, REMOTE_DATA_DIR, self.fmtParser.FmtCode)
        self.DATADIR = pjoin(self.repo_path, DATA_DIR, self.fmtParser.FmtCode)
        self.STOREDIR = pjoin(self.repo_path, STORE_DATA_DIR, self.fmtParser.FmtCode)
        if not os.path.isdir(self.DATADIR):
            os.makedirs(self.DATADIR)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.w_uid in self.wFp:
            self.wFp[self.w_uid]['/'].attrs.modify('next_dset', (self.hNextPath, self.hIdx))
            self.wFp[self.w_uid]['/'].attrs.modify('num_collections_remaining', self.hColsRemain)
            self.wFp[self.w_uid].flush()

    @staticmethod
    def _dataset_opts(complib, complevel, shuffle, fletcher32):
        '''specify compression options for the hdf5 dataset.

        .. seealso:: :function:`_blosc_opts`

        to enable blosc compression, use the conda-forge `blosc-hdf5-plugin` package.

        .. seealso::

        * https://github.com/conda-forge/staged-recipes/pull/7650
        * https://github.com/h5py/h5py/issues/611

        Parameters
        ----------
        complib : str
            the compression lib to use, one of ['lzf', 'gzip', 'blosc:blosclz',
            'blosc:lz4', 'blosc:lz4hc', 'blosc:snappy', 'blosc:zlib', 'blosc:zstd']
        complevel : int
            compression level to specify (accepts values [0, 9] for all except 'lzf'
            where no complevel is accepted)
        shuffle : bool
            if True, enable byte shuffle filter, if blosc compression, pass through
            'bits' is accepted as well.
        fletcher32 : bool
            enable fletcher32 checksum validation of data integrity, (defaults to
            True, which is enabled)
        '''
        if complib.startswith('blosc'):
            shuffle = 2 if shuffle == 'bit' else 1 if shuffle else 0
            compressors = ['blosclz', 'lz4', 'lz4hc', 'snappy', 'zlib', 'zstd']
            complib = ['blosc:' + c for c in compressors].index(complib)
            args = {
                'compression': 32001,
                'compression_opts': (0, 0, 0, 0, complevel, shuffle, complib),
                'fletcher32': fletcher32,
            }
            if shuffle:
                args['shuffle'] = False
        else:
            args = {
                'shuffle': shuffle,
                'compression': complib,
                'compression_opts': None if complib == 'lzf' else complevel,
                'fletcher32': fletcher32,
            }
        return args

    @staticmethod
    def _chunk_opts(sample_array, max_chunk_nbytes):
        '''Determine the chunk shape so each array chunk fits into configured nbytes.

        Currently the chunk nbytes are not user configurable. Instead the constant
        `HDF5_MAX_CHUNK_NBYTES` is sued to determine when to split.

        Parameters
        ----------
        sample_array : `np.array`
            Sample array whose shape and dtype should be used as the basis of the
            chunk shape determination
        max_chunk_nbytes : int
            how many bytes the array chunks should be limited to.

        Returns
        -------
        list
            list of ints of length == rank of `sample_array` specifying chunk sizes
            to split `sample_array` into nbytes
        int
            nbytes which the chunk will fit in. Will be <= `HDF5_MAX_CHUNK_NBYTES`
        '''
        chunk_nbytes = sample_array.nbytes
        chunk_shape = list(sample_array.shape)
        shape_rank = len(chunk_shape)
        chunk_idx = 0

        while chunk_nbytes > max_chunk_nbytes:
            if chunk_idx >= shape_rank:
                chunk_idx = 0
            rank_dim = chunk_shape[chunk_idx]
            if rank_dim <= 2:
                chunk_idx += 1
                continue
            chunk_shape[chunk_idx] = math.floor(rank_dim / 2)
            chunk_nbytes = np.zeros(shape=chunk_shape, dtype=sample_array.dtype).nbytes
            chunk_idx += 1

        return (chunk_shape, chunk_nbytes)

    def create_schema(self, *, remote_operation: bool = False) -> h5py.File:
        '''stores the shape and dtype as the schema of a dataset.

        Parameters
        ----------
        repo_path : str
            path where the repository files can be accessed on the local disk.
        uid : str
            file name prefix for the hdf5 file
        sample_array : np.ndarray
            sample input tensor (representitative of all data which will fill the dset) to
            extract the shape and dtype from.
        remote_operation : optional, kwarg only, bool
            if this schema is being created from a remote fetch operation, then do not
            place the file symlink in the staging directory. Instead symlink it
            to a special remote staging directory. (default is False, which places the
            symlink in the stage data directory.)

        Returns
        -------
        h5py.File
            File handle to the created h5py file

        Notes
        -----

        Parameters set for raw-data-chunk-cache (rdcc) values:

        * rdcc_nbytes: sets the total size (measured in bytes) of the raw data chunk
          cache for each dataset. This should be set to the size of each chunk times
          the number of chunks that are likely to be needed in cache.
        * rdcc_w0: sets the policy for chunks to be removed from the cache when more
          space is needed. If set to 0, always evict the least recently used chunk in
          cache. If set to 1, always evict the least recently used chunk which has
          been fully read or written. If the value is between 0 and 1, the behavior
          will be a blend of the two.
        * rdcc_nslots: The number of chunk slots in the cache for this entire file.
          In order for quick lookup, a hash map is used for each chunk value. For
          maximum performance, this value should be set approximately 100 times that
          number of chunks.

        .. seealso::

            http://docs.h5py.org/en/stable/high/file.html#chunk-cache

        '''

        # -------------------- Chunk & RDCC Vals ------------------------------

        sample_array = np.zeros(self.schema_shape, dtype=self.schema_dtype)
        chunk_shape, chunk_nbytes = __class__._chunk_opts(
            sample_array=sample_array,
            max_chunk_nbytes=HDF_CHUNK_OPTS['max_nbytes'])

        rdcc_nbytes_val = math.ceil((sample_array.nbytes / chunk_nbytes) * chunk_nbytes * 10)
        if rdcc_nbytes_val < HDF_CHUNK_OPTS['max_nbytes']:
            rdcc_nbytes_val = HDF_CHUNK_OPTS['max_nbytes']
        elif rdcc_nbytes_val > HDF_CHUNK_OPTS['max_rdcc_nbytes']:
            rdcc_nbytes_val = HDF_CHUNK_OPTS['max_rdcc_nbytes']

        rdcc_nslots_guess = math.ceil(rdcc_nbytes_val / chunk_nbytes) * 100
        rdcc_nslots_prime_val = find_next_prime(rdcc_nslots_guess)

        # ---------------------------- File Creation --------------------------

        uid = random_string()
        file_path = pjoin(self.DATADIR, f'{uid}.hdf5')
        logger.debug(f'creating: {file_path}')
        fh = h5py.File(
            file_path,
            mode='w',
            libver='latest',
            rdcc_nbytes=rdcc_nbytes_val,
            rdcc_w0=HDF_CHUNK_OPTS['rdcc_w0'],
            rdcc_nslots=rdcc_nslots_prime_val)

        if remote_operation:
            symlink_file_path = pjoin(self.REMOTEDIR, f'{uid}.hdf5')
        else:
            symlink_file_path = pjoin(self.STAGEDIR, f'{uid}.hdf5')

        symlink_rel(file_path, symlink_file_path)

        # ----------------------- Dataset Creation ----------------------------

        optKwargs = __class__._dataset_opts(**HDF_DSET_FILTERS)

        for dset_num in range(HDF_DSET_CONTENTS['num_collections']):
            fh.create_dataset(
                f'/{dset_num}',
                shape=(HDF_DSET_CONTENTS['collection_size'], *sample_array.shape),
                dtype=sample_array.dtype,
                maxshape=(HDF_DSET_CONTENTS['collection_size'], *sample_array.shape),
                chunks=(1, *chunk_shape),
                **optKwargs)

        # ---------------------- Attribute Config Vals ------------------------

        fh['/'].attrs[HDF_ATTRS['hangar_version']] = __version__
        fh['/'].attrs[HDF_ATTRS['schema_shape']] = sample_array.shape
        fh['/'].attrs[HDF_ATTRS['schema_dtype']] = sample_array.dtype.num
        fh['/'].attrs[HDF_ATTRS['next_location']] = (0, 0)
        fh['/'].attrs[HDF_ATTRS['collection_max_size']] = HDF_DSET_CONTENTS['collection_size']
        fh['/'].attrs[HDF_ATTRS['collection_total']] = HDF_DSET_CONTENTS['num_collections']
        fh['/'].attrs[HDF_ATTRS['collections_remaining']] = HDF_DSET_CONTENTS['num_collections']
        fh['/'].attrs[HDF_ATTRS['rdcc_nbytes']] = rdcc_nbytes_val
        fh['/'].attrs[HDF_ATTRS['rdcc_w0']] = HDF_CHUNK_OPTS['rdcc_w0']
        fh['/'].attrs[HDF_ATTRS['rdcc_nslots']] = rdcc_nslots_prime_val
        fh['/'].attrs[HDF_ATTRS['shuffle']] = optKwargs['shuffle']
        fh['/'].attrs[HDF_ATTRS['complib']] = HDF_DSET_FILTERS['complib']
        fh['/'].attrs[HDF_ATTRS['fletcher32']] = optKwargs['fletcher32']
        fh['/'].attrs[HDF_ATTRS['chunk_shape']] = chunk_shape
        if optKwargs['compression_opts'] is not None:
            fh['/'].attrs[HDF_ATTRS['comp_opts']] = optKwargs['compression_opts']
        else:
            fh['/'].attrs[HDF_ATTRS['comp_opts']] = False

        fh.flush()
        try:
            fh.swmr_mode = True
        except ValueError:
            assert fh.swmr_mode is True

        self.w_uid = uid
        self.wFp[uid] = fh
        self.hNextPath = 0
        self.hIdx = 0
        self.hColsRemain = HDF_DSET_CONTENTS['num_collections']
        self.hMaxSize = HDF_DSET_CONTENTS['collection_size']

    def open(self, mode: str, *, remote_operation: bool = False):
        '''Open an hdf5 file handle in the Handler Singleton

        Parameters
        ----------
        mode : str
            one of `r` or `a` for read only / read-write.
        repote_operation : optional, kwarg only, bool
            if this hdf5 data is being created from a remote fetch operation, then
            we don't open any files for reading, and only open files for writing
            which exist in the remote data dir. (default is false, which means that
            write operations use the stage data dir and read operations use data store
            dir)
        '''
        self.mode = mode
        if self.mode == 'a':
            process_dir = self.REMOTEDIR if remote_operation else self.STAGEDIR
            if not os.path.isdir(process_dir):
                os.makedirs(process_dir)

            process_uids = [x for x in os.listdir(process_dir) if x.endswith('.hdf5')]
            for uid in process_uids:
                file_pth = pjoin(process_dir, f'{uid}.hdf5')
                self.rFp[uid] = partial(h5py.File, file_pth, 'r', swmr=True, libver='latest')

        if not remote_operation:
            if not os.path.isdir(self.STOREDIR):
                return
            store_uids = [x for x in os.listdir(self.STOREDIR) if x.endswith('.hdf5')]
            for uid in store_uids:
                file_pth = pjoin(self.STOREDIR, f'{uid}.hdf5')
                self.rFp[uid] = partial(h5py.File, file_pth, 'r', swmr=True, libver='latest')

    def close(self):
        '''Close a file handle after writes have been completed

        behavior changes depending on write-enable or read-only file

        Returns
        -------
        bool
            True if success, otherwise False.
        '''
        if self.mode == 'a':
            if self.w_uid in self.wFp:
                self.wFp[self.w_uid]['/'].attrs.modify('next_dset', (self.hNextPath, self.hIdx))
                self.wFp[self.w_uid]['/'].attrs.modify('num_collections_remaining', self.hColsRemain)
                self.wFp[self.w_uid].flush()
                self.hMaxSize = None
                self.hNextPath = None
                self.hIdx = None
                self.hColsRemain = None
                self.w_uid = None
            for uid in list(self.wFp.keys()):
                try:
                    self.wFp[uid].close()
                except AttributeError:
                    pass
                del self.wFp[uid]
        else:
            for uid in list(self.rFp.keys()):
                try:
                    self.rFp[uid].close()
                except AttributeError:
                    pass
                del self.rFp[uid]

    @staticmethod
    def remove_unused(repo_path, stagehashenv):
        '''If no changes made to staged hdf files, remove and unlik them from stagedir

        This searchs the stagehashenv file for all schemas & instances, and if any
        files are present in the stagedir without references in stagehashenv, the
        symlinks in stagedir and backing data files in datadir are removed.

        Parameters
        ----------
        repo_path : str
            path to the repository on disk
        stagehashenv : `lmdb.Environment`
            db where all stage hash additions are recorded

        '''
        from ..records.hashs import HashQuery

        FmtCode = HDF5_00_Parser().FmtCode
        FmtBackend = HDF5_00_Parser().FmtBackend
        dat_dir = pjoin(repo_path, DATA_DIR, FmtCode)
        stg_dir = pjoin(repo_path, STAGE_DATA_DIR, FmtCode)
        if not os.path.isdir(stg_dir):
            return

        stgHashs = HashQuery(stagehashenv).list_all_hash_values()
        stg_files = set(v.uid for v in stgHashs if v.backend == FmtBackend)
        stg_uids = set(psplitext(x)[0] for x in os.listdir(stg_dir) if x.endswith('.hdf5'))
        unused_uids = stg_uids.difference(stg_files)

        for unused_uid in unused_uids:
            remove_link_pth = pjoin(stg_dir, f'{unused_uid}.hdf5')
            remove_data_pth = pjoin(dat_dir, f'{unused_uid}.hdf5')
            os.remove(remove_link_pth)
            os.remove(remove_data_pth)

    def write_data(self, array: np.ndarray, *, remote_operation: bool = False) -> bytes:
        '''verifies correctness of array data and performs write operation.

        Parameters
        ----------
        array : np.ndarray
            tensor to write to group.
        remote_operation : optional, kwarg only, bool
            If this is a remote process which is adding data, any necessary
            hdf5 dataset files will be created in the remote data dir instead
            of the stage directory. (default is False, which is for a regular
            access process)

        Returns
        -------
        bytes
            string identifying the collection dataset and collection dim-0 index
            which the array can be accessed at.
        '''
        if self.w_uid in self.wFp:
            self.hIdx += 1
            if self.hIdx >= self.hMaxSize:
                self.hIdx = 0
                self.hNextPath += 1
                self.hColsRemain -= 1
                if self.hColsRemain <= 1:
                    self.wFp[self.w_uid]['/'].attrs.modify('next_dset', (self.hNextPath, self.hIdx))
                    self.wFp[self.w_uid]['/'].attrs.modify('num_collections_remaining', self.hColsRemain)
                    self.wFp[self.w_uid].flush()
                    self.create_schema(remote_operation=remote_operation)
        else:
            self.create_schema(remote_operation=remote_operation)

        srcSlc = None
        destSlc = (self.slcExpr[self.hIdx], *(self.slcExpr[0:x] for x in array.shape))
        self.wFp[self.w_uid][f'/{self.hNextPath}'].write_direct(array, srcSlc, destSlc)

        hashVal = self.fmtParser.encode(uid=self.w_uid,
                                        dataset=self.hNextPath,
                                        dataset_idx=self.hIdx,
                                        shape=array.shape)
        return hashVal

    def read_data(self, hashVal: HDF5_00_Parser.DataHashSpec) -> np.ndarray:
        '''Read data from an hdf5 file handle at the specified locations

        Parameters
        ----------
        hashVal : namedtuple
            record specification stored in the DB.

        Returns
        -------
        np.array
            requested data.
        '''
        dsetIdx = int(hashVal.dataset_idx)
        dsetCol = f'/{hashVal.dataset}'

        srcSlc = (self.slcExpr[dsetIdx], *(self.slcExpr[0:x] for x in hashVal.shape))
        destSlc = None
        destArr = np.empty((hashVal.shape), self.schema_dtype)

        try:
            self.Fp[hashVal.uid][dsetCol].read_direct(destArr, srcSlc, destSlc)
        except TypeError:
            self.Fp[hashVal.uid] = self.Fp[hashVal.uid]()
            self.Fp[hashVal.uid][dsetCol].read_direct(destArr, srcSlc, destSlc)

        return destArr
