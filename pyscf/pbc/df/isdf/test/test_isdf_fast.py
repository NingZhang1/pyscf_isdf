#!/usr/bin/env python
# Copyright 2014-2020 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Ning Zhang <ningzhang1024@gmail.com>
#

import copy
from functools import reduce
import numpy as np
from pyscf import lib
import pyscf.pbc.gto as pbcgto
from pyscf.pbc.gto import Cell
from pyscf.pbc import tools
from pyscf.pbc.lib.kpts import KPoints
from pyscf.pbc.lib.kpts_helper import is_zero, gamma_point, member
from pyscf.gto.mole import *
from pyscf.pbc.df.isdf.isdf_jk import _benchmark_time
import pyscf.pbc.df.isdf.isdf_ao2mo as isdf_ao2mo
import pyscf.pbc.df.isdf.isdf_jk as isdf_jk

import sys
import ctypes
import _ctypes

from multiprocessing import Pool

import dask.array as da
from dask import delayed

from memory_profiler import profile

libpbc = lib.load_library('libpbc')
def _fpointer(name):
    return ctypes.c_void_p(_ctypes.dlsym(libpbc._handle, name))

BASIS_CUTOFF = 1e-18  # too small may lead to numerical instability
CRITERION_CALL_PARALLEL_QR = 256

# python version colpilot_qr() function

@delayed
def _vec_norm(vec):
    return np.linalg.norm(vec)
@delayed
def _daxpy(a, x, y):
    return y + a * x

def _colpivot_qr_parallel(A, max_rank=None, cutoff=1e-14):
    m, n = A.shape
    Q = np.zeros((m, m))
    R = np.zeros((m, n))
    AA = A.T.copy()  # cache friendly
    pivot = np.arange(n)

    if max_rank is None:
        max_rank = min(m, n)

    npt_find = 0

    for j in range(min(m, n, max_rank)):
        # Find the column with the largest norm

        # norms = np.linalg.norm(AA[j:, :], axis=1)
        task_norm = []
        for i in range(j, n):
            task_norm.append(_vec_norm(AA[i, :]))
        norms = da.compute(*task_norm)
        norms = np.asarray(norms)
        p = np.argmax(norms) + j

        # Swap columns j and p

        AA[[j, p], :] = AA[[p, j], :]
        R[:, [j, p]] = R[:, [p, j]]
        pivot[[j, p]] = pivot[[p, j]]

        # perform Shimdt orthogonalization

        R[j, j] = np.linalg.norm(AA[j, :])
        if R[j, j] < cutoff:
            break
        npt_find += 1
        Q[j, :] = AA[j, :] / R[j, j]

        R[j, j + 1:] = np.dot(AA[j + 1:, :], Q[j, :].T)
        # AA[j + 1:, :] -= np.outer(R[j, j + 1:], Q[j, :])
        task_daxpy = []
        for i in range(j + 1, n):
            task_daxpy.append(_daxpy(-R[j, i], Q[j, :], AA[i, :]))
        if len(task_daxpy) > 0:
            res = da.compute(*task_daxpy)
            AA[j + 1:, :] = np.concatenate(da.compute(res), axis=0)

    return Q.T, R, pivot, npt_find

def colpivot_qr(A, max_rank=None, cutoff=1e-14):
    '''
    we do not need Q
    '''

    m, n = A.shape
    Q = np.zeros((m, m))
    R = np.zeros((m, n))
    AA = A.T.copy()  # cache friendly
    pivot = np.arange(n)

    if max_rank is None:
        max_rank = min(m, n)

    npt_find = 0

    for j in range(min(m, n, max_rank)):
        # Find the column with the largest norm

        # norms = np.linalg.norm(AA[:, j:], axis=0)
        norms = np.linalg.norm(AA[j:, :], axis=1)
        p = np.argmax(norms) + j

        # Swap columns j and p

        # AA[:, [j, p]] = AA[:, [p, j]]
        AA[[j, p], :] = AA[[p, j], :]
        R[:, [j, p]] = R[:, [p, j]]
        pivot[[j, p]] = pivot[[p, j]]

        # perform Shimdt orthogonalization

        # R[j, j] = np.linalg.norm(AA[:, j])
        R[j, j] = np.linalg.norm(AA[j, :])
        if R[j, j] < cutoff:
            break
        npt_find += 1
        # Q[:, j] = AA[:, j] / R[j, j]
        Q[j, :] = AA[j, :] / R[j, j]

        # R[j, j + 1:] = np.dot(Q[:, j].T, AA[:, j + 1:])
        R[j, j + 1:] = np.dot(AA[j + 1:, :], Q[j, :].T)
        # AA[:, j + 1:] -= np.outer(Q[:, j], R[j, j + 1:])
        AA[j + 1:, :] -= np.outer(R[j, j + 1:], Q[j, :])

    return Q.T, R, pivot, npt_find

@delayed
def atm_IP_task(taskinfo:tuple):
    grid_ID, aoR_atm, nao, nao_atm, c, m = taskinfo

    npt_find = c * nao_atm + 10
    naux_tmp = int(np.sqrt(c*nao_atm)) + m
    # generate to random orthogonal matrix of size (naux_tmp, nao), do not assume sparsity here
    if naux_tmp > nao:
        aoR_atm1 = aoR_atm
        aoR_atm2 = aoR_atm
    else:
        G1 = np.random.rand(nao, naux_tmp)
        G1, _ = numpy.linalg.qr(G1)
        G2 = np.random.rand(nao, naux_tmp)
        G2, _ = numpy.linalg.qr(G2)
        aoR_atm1 = G1.T @ aoR_atm
        aoR_atm2 = G2.T @ aoR_atm
    aoPair = np.einsum('ik,jk->ijk', aoR_atm1, aoR_atm2).reshape(-1, grid_ID.shape[0])
    _, R, pivot, npt_find = colpivot_qr(aoPair, max_rank=npt_find)
    # npt_find = min(R.shape[0], R.shape[1])
    pivot_ID = grid_ID[pivot[:npt_find]]  # the global ID
    # pack res
    return pivot_ID, pivot[:npt_find], R[:npt_find, :npt_find], npt_find

@delayed
def partition_IP_task(taskinfo:tuple):
    grid_ID, aoR_atm, nao, naux, m = taskinfo

    npt_find = naux
    naux_tmp = int(np.sqrt(naux)) + m
    # generate to random orthogonal matrix of size (naux_tmp, nao), do not assume sparsity here
    if naux_tmp > nao:
        aoR_atm1 = aoR_atm
        aoR_atm2 = aoR_atm
    else:
        G1 = np.random.rand(nao, naux_tmp)
        G1, _ = numpy.linalg.qr(G1)
        G2 = np.random.rand(nao, naux_tmp)
        G2, _ = numpy.linalg.qr(G2)
        aoR_atm1 = G1.T @ aoR_atm
        aoR_atm2 = G2.T @ aoR_atm
    aoPair = np.einsum('ik,jk->ijk', aoR_atm1, aoR_atm2).reshape(-1, grid_ID.shape[0])
    _, R, pivot, npt_find = colpivot_qr(aoPair, max_rank=npt_find)
    # npt_find = min(R.shape[0], R.shape[1])
    pivot_ID = grid_ID[pivot[:npt_find]]  # the global ID
    # pack res
    return pivot_ID, pivot[:npt_find], R[:npt_find, :npt_find], npt_find

@delayed
def construct_local_basis(taskinfo:tuple):
    # IP_local_ID, aoR_atm, naoatm, c = taskinfo
    IP_local_ID, aoR_atm, naux = taskinfo

    # naux = naoatm * c
    assert IP_local_ID.shape[0] >= naux
    IP_local_ID = IP_local_ID[:naux]

    IP_local_ID.sort()
    aoRg = aoR_atm[:, IP_local_ID]
    A = np.asarray(lib.dot(aoRg.T, aoRg), order='C')
    A = A ** 2
    B = np.asarray(lib.dot(aoRg.T, aoR_atm), order='C')
    B = B ** 2

    e, h = np.linalg.eigh(A)
    # remove those eigenvalues that are too small
    where = np.where(abs(e) > BASIS_CUTOFF)[0]
    e = e[where]
    h = h[:, where]
    aux_basis = np.asarray(lib.dot(h.T, B), order='C')
    aux_basis = (1.0/e).reshape(-1, 1) * aux_basis
    aux_basis = np.asarray(lib.dot(h, aux_basis), order='C')

    return IP_local_ID, aux_basis

'''
/// the following variables are input variables
    int nao;
    int natm;
    int ngrids;
    double cutoff_aoValue;
    const int *ao2atomID;
    const double *aoG;
    double cutoff_QR;
/// the following variables are output variables
    int *voronoi_partition;
    int *ao_sparse_rep_row;
    int *ao_sparse_rep_col;
    double *ao_sparse_rep_val;
    int naux;
    int *IP_index;
    double *auxiliary_basis;

'''
class _PBC_ISDF(ctypes.Structure):
    _fields_ = [('nao', ctypes.c_int),
                ('natm', ctypes.c_int),
                ('ngrids', ctypes.c_int),
                ('cutoff_aoValue', ctypes.c_double),
                ('cutoff_QR', ctypes.c_double),
                ('naux', ctypes.c_int),
                ('ao2atomID', ctypes.c_void_p),
                ('aoG', ctypes.c_void_p),
                ('voronoi_partition', ctypes.c_void_p),
                ('ao_sparse_rep_row', ctypes.c_void_p),
                ('ao_sparse_rep_col', ctypes.c_void_p),
                ('ao_sparse_rep_val', ctypes.c_void_p),
                ('IP_index', ctypes.c_void_p),
                ('auxiliary_basis', ctypes.c_void_p)
                ]

from pyscf.pbc import df

class PBC_ISDF_Info(df.fft.FFTDF):

    def __init__(self, mol:Cell, aoR: np.ndarray = None,
                 cutoff_aoValue: float = 1e-12,
                 cutoff_QR: float = 1e-8):

        super().__init__(cell=mol)

        self._this = ctypes.POINTER(_PBC_ISDF)()

        ## the following variables are used in build_sandeep

        self.IP_ID     = None
        self.aux_basis = None
        self.c         = None
        self.naux      = None
        self.W         = None
        self.aoRg      = None
        self.aoR       = aoR
        if aoR is not None:
            self.aoRT  = aoR.T
        else:
            self.aoRT  = None
        self.V_R       = None
        self.cell      = mol

        self.partition = None

        self.natm = mol.natm
        self.nao = mol.nao_nr()

        from pyscf.pbc.dft.multigrid.multigrid_pair import MultiGridFFTDF2, _eval_rhoG

        df_tmp = None
        if aoR is None:
            df_tmp = MultiGridFFTDF2(mol)
            self.coords = np.asarray(df_tmp.grids.coords).reshape(-1,3)
            self.ngrids = self.coords.shape[0]
        else:
            self.ngrids = aoR.shape[1]
            assert self.nao == aoR.shape[0]

        ## preallocated buffer for parallel calculation

        self.jk_buffer = None
        self.ddot_buf  = None

        ao2atomID = np.zeros(self.nao, dtype=np.int32)
        ao2atomID = np.zeros(self.nao, dtype=np.int32)

        # only valid for spherical GTO

        ao_loc = 0
        for i in range(mol._bas.shape[0]):
            atm_id = mol._bas[i, ATOM_OF]
            nctr   = mol._bas[i, NCTR_OF]
            angl   = mol._bas[i, ANG_OF]
            nao_now = nctr * (2 * angl + 1)  # NOTE: sph basis assumed!
            ao2atomID[ao_loc:ao_loc+nao_now] = atm_id
            ao_loc += nao_now

        print("ao2atomID = ", ao2atomID)

        self.ao2atomID = ao2atomID
        self.ao2atomID = ao2atomID

        # given aoG, determine at given grid point, which ao has the maximal abs value

        if aoR is not None:
            self.partition = np.argmax(np.abs(aoR), axis=0)
            print("partition = ", self.partition.shape)
            # map aoID to atomID
            self.partition = np.asarray([ao2atomID[x] for x in self.partition])
            self.coords    = None
            self._numints  = None
        else:
            grids   = df_tmp.grids
            coords  = np.asarray(grids.coords).reshape(-1,3)
            NumInts = df_tmp._numint

            self.partition = np.zeros(coords.shape[0], dtype=np.int32)
            MAX_MEMORY = 2 * 1e9  # 2 GB
            bunchsize  = int(MAX_MEMORY / (self.nao * 8))  # 8 bytes per double
            assert bunchsize > 0
            # buf = np.array((MAX_MEMORY//8), dtype=np.double) # note the memory has to be allocated here, one cannot optimize it!
            for p0, p1 in lib.prange(0, coords.shape[0], bunchsize):
                res = NumInts.eval_ao(mol, coords[p0:p1])[0].T
                res = np.argmax(np.abs(res), axis=0)
                self.partition[p0:p1] = np.asarray([ao2atomID[x] for x in res])
            res = None
            self.coords = coords
            self._numints = NumInts

        # cached jk and dm
                
        # NOTE: it seems that the linearity of JK w.r.t dm is not fully explored in pbc/df module 
                
        self._cached_dm = None
        self._cached_j  = None
        self._cached_k  = None

        # check the sparsity 

        self._check_sparsity      = True
        self._explore_sparsity    = False
        self._dm_cutoff           = 1e-8
        self._rho_on_grid_cutoff  = 1e-10
        self._dm_on_grid_cutoff   = 1e-10

        self.dm_RowNElmt = None
        self.dm_RowLoc   = None
        self.dm_ColIndx  = None
        self.dm_Elmt     = None
        self.K_Indx      = None

        self.dm_compressed = False

    # @profile

    def _allocate_jk_buffer(self, datatype):

        if self.jk_buffer is None:

            nao    = self.nao
            ngrids = self.ngrids
            naux   = self.naux

            buffersize_k = nao * ngrids + naux * ngrids + naux * naux + nao * nao
            buffersize_j = nao * ngrids + ngrids + nao * naux + naux + naux + nao * nao

            self.jk_buffer = np.ndarray((max(buffersize_k, buffersize_j),), dtype=datatype)
            # self.jk_buffer[-1] = 0.0 # memory allocate, well, you cannot cheat python in this way

            print("address of self.jk_buffer = ", id(self.jk_buffer))

            nThreadsOMP = lib.num_threads()
            print("nThreadsOMP = ", nThreadsOMP)
            self.ddot_buf = np.zeros((nThreadsOMP,max((naux*naux)+2, ngrids)), dtype=datatype)
            # self.ddot_buf[nThreadsOMP-1, (naux*naux)+1] = 0.0 # memory allocate, well, you cannot cheat python in this way

        else:
            assert self.jk_buffer.dtype == datatype
            assert self.ddot_buf.dtype == datatype

    def _allocate_dm_sparse_handler(self, datatype):

        if self.dm_RowNElmt is None:
            self.dm_RowNElmt = np.ndarray((self.nao,), dtype=datatype)
            self.dm_RowLoc   = np.ndarray((self.nao,), dtype=np.int32)
            self.dm_ColIndx  = np.ndarray((self.nao*self.nao,), dtype=np.int32)
            self.dm_Elmt     = np.ndarray((self.nao*self.nao,), dtype=datatype)
            self.K_Indx      = np.ndarray((self.naux,), dtype=np.int32)

    def build(self):
        raise NotImplementedError
        # print("warning: not implemented yet")

    def build_only_partition(self):
        raise NotImplementedError
        # print("warning: not implemented yet")


    # @profile


    def get_A_B(self):

        aoR   = self.aoR
        IP_ID = self.IP_ID
        aoRG  = aoR[:, IP_ID]

        A = np.asarray(lib.dot(aoRG.T, aoRG), order='C')
        A = A ** 2
        B = np.asarray(lib.dot(aoRG.T, aoR), order='C')
        B = B ** 2

        return A, B


    def build_IP_Sandeep(self, c=5, m=5,
                         ratio=0.8,
                         global_IP_selection=True,
                         build_global_basis=True, debug=True):

        # build partition

        ao2atomID = self.ao2atomID
        partition = self.partition
        aoR  = self.aoR
        natm = self.natm
        nao  = self.nao
        ao2atomID = self.ao2atomID
        partition = self.partition
        aoR  = self.aoR
        natm = self.natm
        nao  = self.nao

        nao_per_atm = np.zeros(natm, dtype=np.int32)
        for i in range(self.nao):
            atm_id = ao2atomID[i]
            nao_per_atm[atm_id] += 1

        # for each atm

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())

        possible_IP = []

        # pack input info for each process

        grid_partition = []
        taskinfo = []

        for atm_id in range(natm):
            # find partition for this atm
            grid_ID = np.where(partition == atm_id)[0]
            grid_partition.append(grid_ID)
            # get aoR for this atm
            aoR_atm = aoR[:, grid_ID]
            nao_atm = nao_per_atm[atm_id]
            taskinfo.append(atm_IP_task((grid_ID, aoR_atm, nao, nao_atm, c, m)))

        results = da.compute(*taskinfo)

        if build_global_basis:

            # collect results

            for atm_id, result in enumerate(results):
                pivot_ID, _, R, npt_find = result

                if global_IP_selection == False:
                    nao_atm  = nao_per_atm[atm_id]
                    naux_now = c * nao_atm
                    pivot_ID = pivot_ID[:naux_now]
                    npt_find = naux_now
                possible_IP.extend(pivot_ID.tolist())


                print("atm_id = ", atm_id)
                print("npt_find = ", npt_find)
                # npt_find = min(R.shape[0], R.shape[1])
                for i in range(npt_find):
                    try:
                        print("R[%3d] = %15.8e" % (i, R[i, i]))
                    except:
                        break

            # sort the possible_IP

            possible_IP.sort()
            possible_IP = np.array(possible_IP)

            t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
            if debug:
                _benchmark_time(t1, t2, "build_IP_Atm")
            t1 = t2

            # a final global QRCP， which is not needed!

            if global_IP_selection:
                aoR_IP = aoR[:, possible_IP]
                naux_tmp = int(np.sqrt(c*nao)) + m
                if naux_tmp > nao:
                    aoR1 = aoR_IP
                    aoR2 = aoR_IP
                else:
                    G1 = np.random.rand(nao, naux_tmp)
                    G1, _ = numpy.linalg.qr(G1)
                    G2 = np.random.rand(nao, naux_tmp)
                    G2, _ = numpy.linalg.qr(G2)
                    # aoR1 = G1.T @ aoR_IP
                    # aoR2 = G2.T @ aoR_IP
                    aoR1 = np.asarray(lib.dot(G1.T, aoR_IP), order='C')
                    aoR2 = np.asarray(lib.dot(G2.T, aoR_IP), order='C')
                aoPair = np.einsum('ik,jk->ijk', aoR1, aoR2).reshape(-1, possible_IP.shape[0])
                npt_find = c * nao

                _, R, pivot, npt_find = colpivot_qr(aoPair, max_rank=npt_find)

                print("global QRCP")
                print("npt_find = ", npt_find)
                # npt_find = min(R.shape[0], R.shape[1]) # may be smaller than c*nao
                for i in range(npt_find):
                    print("R[%3d] = %15.8e" % (i, R[i, i]))

                IP_ID = possible_IP[pivot[:npt_find]]
            else:
                IP_ID = possible_IP

            IP_ID.sort()
            print("IP_ID = ", IP_ID)
            self.IP_ID = IP_ID

            t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
            if debug:
                _benchmark_time(t1, t2, "build_IP_Global")
            t1 = t2

            # build the auxiliary basis

            # allocate memory for the auxiliary basis

            naux = IP_ID.shape[0]
            self.naux = naux
            self._allocate_jk_buffer(datatype=np.double)
            buffer1 = np.ndarray((self.naux , self.naux), dtype=np.double, buffer=self.jk_buffer, offset=0)

            ## TODO: optimize this code so that the memory allocation is minimal!

            aoRg = numpy.empty((nao, IP_ID.shape[0]))
            lib.dslice(aoR, IP_ID, out=aoRg)
            A = np.asarray(lib.ddot(aoRg.T, aoRg, c=buffer1), order='C')  # buffer 1 size = naux * naux
            lib.square_inPlace(A)

            self.aux_basis = np.asarray(lib.ddot(aoRg.T, aoR), order='C')   # buffer 2 size = naux * ngrids
            lib.square_inPlace(self.aux_basis)

            fn_cholesky = getattr(libpbc, "Cholesky", None)
            assert(fn_cholesky is not None)

            fn_build_aux = getattr(libpbc, "Solve_LLTEqualB_Parallel", None)
            assert(fn_build_aux is not None)

            fn_cholesky(
                A.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(naux),
            )
            nThread = lib.num_threads()
            nGrids  = aoR.shape[1]
            Bunchsize = nGrids // nThread
            fn_build_aux(
                ctypes.c_int(naux),
                A.ctypes.data_as(ctypes.c_void_p),
                self.aux_basis.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(nGrids),
                ctypes.c_int(Bunchsize)
            )

            # use diagonalization instead, but too slow for large system
            # e, h = np.linalg.eigh(A)  # single thread, but should not be slow, it should not be the bottleneck
            # print("e[-1] = ", e[-1])
            # print("e[0]  = ", e[0])
            # print("condition number = ", e[-1]/e[0])
            # for id, val in enumerate(e):
            #     print("e[%5d] = %15.8e" % (id, val))
            # # remove those eigenvalues that are too small
            # where = np.where(abs(e) > BASIS_CUTOFF)[0]
            # e = e[where]
            # h = h[:, where]
            # print("e.shape = ", e.shape)
            # # self.aux_basis = h @ np.diag(1/e) @ h.T @ B
            # # self.aux_basis = np.asarray(lib.dot(h.T, B), order='C')  # maximal size = naux * ngrids
            # buffer2 = np.ndarray((e.shape[0] , self.ngrids), dtype=np.double, buffer=self.jk_buffer,
            #          offset=self.naux * self.naux * self.jk_buffer.dtype.itemsize)
            # B = np.asarray(lib.ddot(h.T, self.aux_basis, c=buffer2), order='C')
            # # self.aux_basis = (1.0/e).reshape(-1, 1) * self.aux_basis
            # # B = (1.0/e).reshape(-1, 1) * B
            # lib.d_i_ij_ij(1.0/e, B, out=B)
            # np.asarray(lib.ddot(h, B, c=self.aux_basis), order='C')

            t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
            if debug:
                _benchmark_time(t1, t2, "build_auxiliary_basis")
        else:

            raise NotImplementedError

        self.c    = c
        self.naux = naux
        self.aoRg = aoRg
        self.aoR  = aoR

    # @profile
    def build_auxiliary_Coulomb(self, cell:Cell, mesh, debug=True):

        # build the ddot buffer

        ngrids = self.ngrids
        ngrids = self.ngrids
        naux   = self.naux

        @delayed
        def construct_V(input:np.ndarray, ngrids, mesh, coul_G, axes=None):
            return (np.fft.ifftn((np.fft.fftn(input, axes=axes).reshape(-1, ngrids) * coul_G[None,:]).reshape(*mesh), axes=axes).real).reshape(ngrids)

        def constrcuct_V_CCode(aux_basis:np.ndarray, mesh, coul_G):
            coulG_real         = coul_G.reshape(*mesh)[:, :, :mesh[2]//2+1].reshape(-1)
            nThread            = lib.num_threads()
            bunchsize          = naux // (2*nThread)
            bufsize_per_thread = bunchsize * coulG_real.shape[0] * 2
            bufsize_per_thread = (bufsize_per_thread + 15) // 16 * 16
            nAux               = aux_basis.shape[0]
            ngrids             = aux_basis.shape[1]
            mesh_int32         = np.array(mesh, dtype=np.int32)

            V                  = np.zeros((nAux, ngrids), dtype=np.double)

            fn = getattr(libpbc, "_construct_V", None)
            assert(fn is not None)

            print("V.shape = ", V.shape)
            print("aux_basis.shape = ", aux_basis.shape)
            print("self.jk_buffer.size    = ", self.jk_buffer.size)
            print("self.jk_buffer.shape   = ", self.jk_buffer.shape)

            fn(mesh_int32.ctypes.data_as(ctypes.c_void_p),
               ctypes.c_int(nAux),
               aux_basis.ctypes.data_as(ctypes.c_void_p),
               coulG_real.ctypes.data_as(ctypes.c_void_p),
               V.ctypes.data_as(ctypes.c_void_p),
               ctypes.c_int(bunchsize),
               self.jk_buffer.ctypes.data_as(ctypes.c_void_p),
               ctypes.c_int(bufsize_per_thread))

            return V

        # print("mesh = ", mesh)

        # ngrids = self.ngrids
        # ngrids = self.ngrids
        # naux   = self.naux

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())

        coulG = tools.get_coulG(cell, mesh=mesh)

        # task = []
        # for i in range(naux):
        #     task.append(construct_V(self.aux_basis[i].reshape(-1,*mesh), ngrids, mesh, coulG))
        # # TODO: change it to C code. preallocate buffer, use fftw3!
        # V_R = np.concatenate(da.compute(*task)).reshape(-1,ngrids)

        V_R = constrcuct_V_CCode(self.aux_basis, mesh, coulG)

        # del task
        coulG = None

        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        if debug:
            _benchmark_time(t1, t2, "build_auxiliary_Coulomb_V_R")
        t1 = t2

        W = np.zeros((naux,naux))
        lib.ddot_withbuffer(a=self.aux_basis, b=V_R.T, buf=self.ddot_buf, c=W, beta=1.0)
        # lib.ddot(self.aux_basis, V_R.T, c=W, beta=1.0)

        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        if debug:
            _benchmark_time(t1, t2, "build_auxiliary_Coulomb_W")

        self.V_R  = V_R
        self.W    = W
        self.mesh = mesh

    def check_AOPairError(self):
        assert(self.aoR is not None)
        assert(self.IP_ID is not None)
        assert(self.aux_basis is not None)

        aoR = self.aoR
        aoRg = aoR[:, self.IP_ID]
        nao = self.nao

        print("In check_AOPairError")

        for i in range(nao):
            coeff = numpy.einsum('k,jk->jk', aoRg[i, :], aoRg).reshape(-1, self.IP_ID.shape[0])
            aoPair = numpy.einsum('k,jk->jk', aoR[i, :], aoR).reshape(-1, aoR.shape[1])
            aoPair_approx = coeff @ self.aux_basis

            diff = aoPair - aoPair_approx
            diff_pair_abs_max = np.max(np.abs(diff), axis=1)

            for j in range(diff_pair_abs_max.shape[0]):
                print("(%5d, %5d, %15.8e)" % (i, j, diff_pair_abs_max[j]))

    def __del__(self):
        try:
            libpbc.PBC_ISDF_del(ctypes.byref(self._this))
        except AttributeError:
            pass

    ##### functions defined in isdf_ao2mo.py #####

    get_eri = get_ao_eri = isdf_ao2mo.get_eri
    ao2mo = get_mo_eri = isdf_ao2mo.general
    ao2mo_7d = isdf_ao2mo.ao2mo_7d  # seems to be only called in kadc and kccsd, NOT implemented!

    ##### functions defined in isdf_jk.py #####

    get_jk = isdf_jk.get_jk_dm

    ##### explore sparsity #####

    def compress_dm(self, dm:np.ndarray):
        self._allocate_dm_sparse_handler(datatype=dm.dtype)

        fn_compress = getattr(libpbc, "_compress_dm", None)
        assert(fn_compress is not None)

        fn_compress(
            dm.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(dm.shape[0]),
            ctypes.c_double(self._dm_cutoff),
            self.dm_RowNElmt.ctypes.data_as(ctypes.c_void_p),
            self.dm_RowLoc.ctypes.data_as(ctypes.c_void_p),
            self.dm_ColIndx.ctypes.data_as(ctypes.c_void_p),
            self.dm_Elmt.ctypes.data_as(ctypes.c_void_p)
        )

        self.dm_compressed = True

    def _dm_aoR_spMM(self, out: np.ndarray):
        fn_dmaoRspMM = getattr(libpbc, "_dm_aoR_spMM", None)
        assert(fn_dmaoRspMM is not None)

        print("_dm_aoR_spMM is called")

        fn_dmaoRspMM(
            self.dm_Elmt.ctypes.data_as(ctypes.c_void_p),
            self.dm_RowLoc.ctypes.data_as(ctypes.c_void_p),
            self.dm_ColIndx.ctypes.data_as(ctypes.c_void_p),
            self.aoR.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(self.nao),
            ctypes.c_int(self.ngrids),
            out.ctypes.data_as(ctypes.c_void_p)
        )

    def process_dm(self, dm:np.ndarray):
        self._allocate_dm_sparse_handler(datatype=dm.dtype)

        nNonZero = ctypes.c_int(0)

        fn_process = getattr(libpbc, "_process_dm", None)
        assert(fn_process is not None)

        fn_process(
            dm.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(dm.shape[0]),
            ctypes.c_double(self._dm_cutoff),
            self.dm_RowNElmt.ctypes.data_as(ctypes.c_void_p),
            ctypes.byref(nNonZero),
        )

        nNonZero = nNonZero.value

        # if nNonZero > self.nao * self.nao * 0.1:
        
        return nNonZero

        #### the following code is not needed, only for test purpose ####

        self._allocate_jk_buffer(datatype=dm.dtype)

        fn_compress = getattr(libpbc, "_compress_dm", None)
        assert(fn_compress is not None)

        fn_compress(
            dm.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(dm.shape[0]),
            ctypes.c_double(self._dm_cutoff),
            self.dm_RowNElmt.ctypes.data_as(ctypes.c_void_p),
            self.dm_RowLoc.ctypes.data_as(ctypes.c_void_p),
            self.dm_ColIndx.ctypes.data_as(ctypes.c_void_p),
            self.dm_Elmt.ctypes.data_as(ctypes.c_void_p)
        )

        #### test dm * aoR, to see the sparsity ####

        res1 = np.zeros((self.nao, self.ngrids))
        res2 = np.zeros_like(res1)

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        lib.ddot(dm, self.aoR, c=res1)  
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        _benchmark_time(t1, t2, "dm aoR ddot")

        fn_dmaoRspMM = getattr(libpbc, "_dm_aoR_spMM", None)
        assert(fn_dmaoRspMM is not None)

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        fn_dmaoRspMM(
            self.dm_Elmt.ctypes.data_as(ctypes.c_void_p),
            self.dm_RowLoc.ctypes.data_as(ctypes.c_void_p),
            self.dm_ColIndx.ctypes.data_as(ctypes.c_void_p),
            self.aoR.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(self.nao),
            ctypes.c_int(self.ngrids),
            res2.ctypes.data_as(ctypes.c_void_p)
        )
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        _benchmark_time(t1, t2, "dm aoR spMM")

        print(np.allclose(res1, res2))

        return nNonZero

    def V_DM_cwise_mul(self, dmDgR:np.ndarray, out: np.ndarray):
        # dmDgR: (naux, ngrids)
        # out: (naux, ngrids)
        # V_DM_cwise_mul(self._this, dmDgR.ctypes.data_as(ctypes.c_void_p), out.ctypes.data_as(ctypes.c_void_p))

        fn = getattr(libpbc, "_cwise_product_check_Sparsity", None)
        assert(fn is not None)

        UseSparsity = ctypes.c_int(0)

        fn(
            self.V_R.ctypes.data_as(ctypes.c_void_p),
            dmDgR.ctypes.data_as(ctypes.c_void_p),
            out.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(self.naux),
            ctypes.c_int(self.ngrids),
            ctypes.c_double(self._dm_on_grid_cutoff),
            self.ddot_buf.ctypes.data_as(ctypes.c_void_p),
            ctypes.byref(UseSparsity),
            self.K_Indx.ctypes.data_as(ctypes.c_void_p)
        )

        return UseSparsity.value

    def V_DM_product_spMM(self, product:np.ndarray, out: np.ndarray):
        fn = getattr(libpbc, "_V_Dm_product_SpMM2", None)
        assert(fn is not None)

        fn(
            product.ctypes.data_as(ctypes.c_void_p),
            self.K_Indx.ctypes.data_as(ctypes.c_void_p),
            self.aoRT.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(self.nao),
            ctypes.c_int(self.naux),
            ctypes.c_int(self.ngrids),
            out.ctypes.data_as(ctypes.c_void_p)
        )

    def _check_V_DM_product_spMM(self, product:np.ndarray, out: np.ndarray):

        fn = getattr(libpbc, "_V_Dm_product_SpMM", None)
        assert(fn is not None)

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        fn(
            product.ctypes.data_as(ctypes.c_void_p),
            self.K_Indx.ctypes.data_as(ctypes.c_void_p),
            self.aoR.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(self.nao),
            ctypes.c_int(self.naux),
            ctypes.c_int(self.ngrids),
            out.ctypes.data_as(ctypes.c_void_p)
        )
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        _benchmark_time(t1, t2, "V_DM_product_spMM")

        fn = getattr(libpbc, "_V_Dm_product_SpMM2", None)
        assert(fn is not None)

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        fn(
            product.ctypes.data_as(ctypes.c_void_p),
            self.K_Indx.ctypes.data_as(ctypes.c_void_p),
            self.aoRT.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(self.nao),
            ctypes.c_int(self.naux),
            ctypes.c_int(self.ngrids),
            out.ctypes.data_as(ctypes.c_void_p)
        )
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        _benchmark_time(t1, t2, "V_DM_product_spMM2")


        tmp = np.zeros_like(self.V_R)
        res = np.zeros((self.naux, self.nao))

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        lib.ddot_withbuffer(tmp, self.aoR.T, c=res, buf=self.ddot_buf)
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())

        _benchmark_time(t1, t2, "V_DM_product_MM")

if __name__ == '__main__':

    # Test the function

    A = np.random.rand(16, 16)
    Q, R, pivot, _ = _colpivot_qr_parallel(A)

    print("A = ", A)
    print("Q = ", Q)
    print("R = ", R)
    print("Q@R = ", Q@R)
    print("A * pivot = ", A[:, pivot])
    print("pivot = ", pivot)
    print("inverse P = ", np.argsort(pivot))
    print("Q * R * inverse P = ", Q@R[:, np.argsort(pivot)])
    print("diff = ", Q@R[:, np.argsort(pivot)] - A)
    print("Q^T * Q = ", Q.T @ Q)

    # exit(1)

    cell   = pbcgto.Cell()
    boxlen = 3.5668
    cell.a = np.array([[boxlen,0.0,0.0],[0.0,boxlen,0.0],[0.0,0.0,boxlen]])

    cell.atom = '''
                   C     0.      0.      0.
                   C     0.8917  0.8917  0.8917
                   C     1.7834  1.7834  0.
                   C     2.6751  2.6751  0.8917
                   C     1.7834  0.      1.7834
                   C     2.6751  0.8917  2.6751
                   C     0.      1.7834  1.7834
                   C     0.8917  2.6751  2.6751
                '''

    # cell.atom = '''
    #                C     0.8917  0.8917  0.8917
    #                C     2.6751  2.6751  0.8917
    #                C     2.6751  0.8917  2.6751
    #                C     0.8917  2.6751  2.6751
    #             '''

    cell.basis   = 'gth-dzv'
    # cell.basis   = 'gth-tzvp'
    cell.pseudo  = 'gth-pade'
    cell.verbose = 4

    cell.ke_cutoff  = 256   # kinetic energy cutoff in a.u.
    # cell.ke_cutoff = 25
    cell.max_memory = 800  # 800 Mb
    cell.precision  = 1e-8  # integral precision
    cell.use_particle_mesh_ewald = True

    cell.build()

    cell = tools.super_cell(cell, [1, 1, 1])

    from pyscf.pbc.dft.multigrid.multigrid_pair import MultiGridFFTDF2, _eval_rhoG

    df_tmp = MultiGridFFTDF2(cell)

    grids  = df_tmp.grids
    coords = np.asarray(grids.coords).reshape(-1,3)
    nx = grids.mesh[0]

    # for i in range(coords.shape[0]):
    #     print(coords[i])
    # exit(1)

    mesh   = grids.mesh
    ngrids = np.prod(mesh)
    assert ngrids == coords.shape[0]

    aoR   = df_tmp._numint.eval_ao(cell, coords)[0].T  # the T is important
    aoR  *= np.sqrt(cell.vol / ngrids)

    print("aoR.shape = ", aoR.shape)

    pbc_isdf_info = PBC_ISDF_Info(cell, aoR, cutoff_aoValue=1e-6, cutoff_QR=1e-3)
    pbc_isdf_info.build_IP_Sandeep(build_global_basis=True, c=15, global_IP_selection=False)
    pbc_isdf_info.build_auxiliary_Coulomb(cell, mesh)
    # pbc_isdf_info.check_AOPairError()

    ### check eri ###

    # mydf_eri = df.FFTDF(cell)
    # eri = mydf_eri.get_eri(compact=False).reshape(cell.nao, cell.nao, cell.nao, cell.nao)
    # print("eri.shape  = ", eri.shape)
    # eri_isdf = pbc_isdf_info.get_eri(compact=False).reshape(cell.nao, cell.nao, cell.nao, cell.nao)
    # print("eri_isdf.shape  = ", eri_isdf.shape)
    # for i in range(cell.nao):
    #     for j in range(cell.nao):
    #         for k in range(cell.nao):
    #             for l in range(cell.nao):
    #                 if abs(eri[i,j,k,l] - eri_isdf[i,j,k,l]) > 1e-6:
    #                     print("eri[{}, {}, {}, {}] = {} != {}".format(i,j,k,l,eri[i,j,k,l], eri_isdf[i,j,k,l]),
    #                           "ration = ", eri[i,j,k,l]/eri_isdf[i,j,k,l])

    ### perform scf ###

    from pyscf.pbc import scf

    mf = scf.RHF(cell)
    pbc_isdf_info.direct_scf = mf.direct_scf
    # pbc_isdf_info._explore_sparsity = True
    mf.with_df = pbc_isdf_info
    mf.max_cycle = 100
    mf.conv_tol = 1e-7

    print("mf.direct_scf = ", mf.direct_scf)

    mf.kernel()

    mf = scf.RHF(cell)
    mf.max_cycle = 100
    mf.conv_tol = 1e-8
    mf.kernel()

