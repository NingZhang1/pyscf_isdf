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

import ctypes
from multiprocessing import Pool
from memory_profiler import profile
libpbc = lib.load_library('libpbc')
from pyscf.pbc.df.isdf.isdf_eval_gto import ISDF_eval_gto 

import pyscf.pbc.df.isdf.isdf_k as ISDF_K
import pyscf.pbc.df.isdf.isdf_linear_scaling as ISDF_LinearScaling
import pyscf.pbc.df.isdf.isdf_linear_scaling_base as ISDF_LinearScalingBase
from pyscf.pbc.df.isdf.isdf_linear_scaling_k_jk import get_jk_dm_k_quadratic
from pyscf.pbc.df.isdf.isdf_fast import rank, comm, comm_size, allgather, bcast, reduce, gather, alltoall, _comm_bunch, allgather_pickle

##### deal with translation symmetry #####

def _expand_partition_prim(partition_prim, kmesh, mesh):

    meshPrim = np.array(mesh) // np.array(kmesh) 
    
    partition = []
    
    for i in range(kmesh[0]):
        for j in range(kmesh[1]):
            for k in range(kmesh[2]):
                shift = i * meshPrim[0] * mesh[1] * mesh[2] + j * meshPrim[1] * mesh[2] + k * meshPrim[2]
                for data in partition_prim:
                    partition.append(data + shift)
    
    return partition

def _expand_primlist_2_superlist(primlist, kmesh, mesh):
    
    meshPrim = np.array(mesh) // np.array(kmesh)
    
    superlist = []
    
    for i in range(kmesh[0]):
        for j in range(kmesh[1]):
            for k in range(kmesh[2]):
                shift = i * meshPrim[0] * mesh[1] * mesh[2] + j * meshPrim[1] * mesh[2] + k * meshPrim[2]
                superlist.extend(primlist + shift)
    
    return np.array(superlist, dtype=np.int32)

def _get_grid_ordering_k(input, kmesh, mesh):
    
    if isinstance(input, list):
        prim_ordering = []
        for data in input:
            prim_ordering.extend(data)
        return _expand_primlist_2_superlist(prim_ordering, kmesh, mesh)
    else:
        raise NotImplementedError

def select_IP_local_ls_k_drive(mydf, c, m, IP_possible_atm, group, use_mpi=False):
    
    IP_group  = []
    aoRg_possible = mydf.aoRg_possible
    
    assert len(IP_possible_atm) == mydf._get_first_natm()
    
    #### do the work ####
    
    first_natm = mydf._get_first_natm()
    
    for i in range(len(group)):
        IP_group.append(None)

    if len(group) < first_natm:
        if use_mpi == False:
            for i in range(len(group)):
                IP_group[i] = ISDF_LinearScaling.select_IP_group_ls(
                    mydf, aoRg_possible, c, m,
                    group = group[i],
                    atm_2_IP_possible=IP_possible_atm
                )
        else:
            group_begin, group_end = ISDF_LinearScalingBase._range_partition(len(group), rank, comm_size, use_mpi)
            for i in range(group_begin, group_end):
                IP_group[i] = ISDF_LinearScaling.select_IP_local_ls(
                    mydf, aoRg_possible, c, m,
                    group = group[i],
                    atm_2_IP_possible=IP_possible_atm
                )
            IP_group = ISDF_LinearScalingBase._sync_list(IP_group, len(group))
    else:
        IP_group = IP_possible_atm

    mydf.IP_group = IP_group
    
    mydf.IP_flat_prim = []
    mydf.IP_segment_prim = []
    nIP_now = 0
    for x in IP_group:
        mydf.IP_flat_prim.extend(x)
        mydf.IP_segment_prim.append(nIP_now)
        nIP_now += len(x)
    mydf.IP_flat = _expand_primlist_2_superlist(mydf.IP_flat_prim, mydf.kmesh, mydf.mesh)
    mydf.naux = mydf.IP_flat.shape[0]
    # mydf.IP_segment = _expand_primlist_2_superlist(mydf.IP_segment_prim[:-1], mydf.kmesh, mydf.mesh)
    # mydf.IP_segment = np.append(mydf.IP_segment, mydf.naux)
    
    gridID_2_atmID = mydf.gridID_2_atmID
    
    partition_IP = []
    for i in range(mydf.cell.natm):
        partition_IP.append([])
    
    for _ip_id_ in mydf.IP_flat:
        atm_id = gridID_2_atmID[_ip_id_]
        partition_IP[atm_id].append(_ip_id_)
    
    for i in range(mydf.cell.natm):
        partition_IP[i] = np.array(partition_IP[i], dtype=np.int32)
    
    mydf.IP_segment = [0]
    for atm_id in mydf.atm_ordering:
        mydf.IP_segment.append(mydf.IP_segment[-1] + len(partition_IP[atm_id]))
    mydf.IP_segment = np.array(mydf.IP_segment, dtype=np.int32)
    
    ### build aoR_IP ###
    
    #### recalculate it anyway ! #### 
    
    coords = mydf.coords
    weight = np.sqrt(mydf.cell.vol / coords.shape[0])
    
    del mydf.aoRg_possible
    mydf.aoRg_possible = None
    
    t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
    
    weight = np.sqrt(mydf.cell.vol / coords.shape[0])
    
    mydf.aoRg = ISDF_LinearScalingBase.get_aoR(
        mydf.cell, coords, partition_IP,
        first_natm,
        mydf.cell.natm,
        mydf.group_global,
        mydf.distance_matrix,
        mydf.AtmConnectionInfo,
        False,
        mydf.use_mpi,
        True)
    
    assert len(mydf.aoRg) == first_natm
    
    mydf.aoRg1 = ISDF_LinearScalingBase.get_aoR(
        mydf.cell, coords, partition_IP,
        mydf.cell.natm,
        first_natm,
        mydf.group_global,
        mydf.distance_matrix,
        mydf.AtmConnectionInfo,
        False,
        mydf.use_mpi,
        True)
    
    assert len(mydf.aoRg1) == mydf.cell.natm
    
    # mydf.aoRg1 = ISDF_LinearScalingBase.get_aoR(
    #     mydf.cell, coords, partition_IP,
    #     first_natm,
    #     mydf.group,
    #     mydf.distance_matrix,
    #     mydf.AtmConnectionInfo,
    #     False,
    #     mydf.use_mpi,
    #     True)
    
    aoRg_activated = []
    for _id_, aoR_holder in enumerate(mydf.aoRg):
        if aoR_holder.ao_involved.size == 0:
            aoRg_activated.append(False)
        else:
            aoRg_activated.append(True)
    aoRg_activated = np.array(aoRg_activated, dtype=bool)
    mydf.aoRg_activated = aoRg_activated
        
    t2 = (lib.logger.process_clock(), lib.logger.perf_counter())

    if rank == 0:
        print("IP_segment = ", mydf.IP_segment)
        print("aoRg memory: ", ISDF_LinearScalingBase._get_aoR_holders_memory(mydf.aoRg))          

def build_auxiliary_Coulomb_local_bas_k(mydf, debug=True, use_mpi=False):
    
    if use_mpi:
        raise NotImplementedError
    
    t0 = (lib.logger.process_clock(), lib.logger.perf_counter())
    
    cell = mydf.cell
    mesh = mydf.mesh
    
    naux = mydf.naux
    
    ncomplex = mesh[0] * mesh[1] * (mesh[2] // 2 + 1) * 2 
    
    grid_ordering = mydf.grid_ID_ordered
    
    coulG = tools.get_coulG(cell, mesh=mesh)
    mydf.coulG = coulG.copy()
    coulG_real = coulG.reshape(*mesh)[:, :, :mesh[2]//2+1].reshape(-1).copy()
    
    nThread = lib.num_threads()
    bufsize_per_thread = int((coulG_real.shape[0] * 2 + mesh[0] * mesh[1] * mesh[2]) * 1.1)
    buf = np.empty((nThread, bufsize_per_thread), dtype=np.double)
    
    def construct_V_CCode(aux_basis:list[np.ndarray], 
                          # buf:np.ndarray, 
                          V=None, shift_row=None):
        
        nThread = buf.shape[0]
        bufsize_per_thread = buf.shape[1]
        
        nAux = 0
        for x in aux_basis:
            nAux += x.shape[0]
        
        ngrids             = mesh[0] * mesh[1] * mesh[2]
        mesh_int32         = np.array(mesh, dtype=np.int32)

        if V is None:
            assert shift_row is None
            V = np.zeros((nAux, ngrids), dtype=np.double)
            
        # V                  = np.zeros((nAux, ngrids), dtype=np.double)
        
        fn = getattr(libpbc, "_construct_V_local_bas", None)
        assert(fn is not None)

        if shift_row is None:
            shift_row = 0
        # ngrid_now = 0
        
        for i in range(len(aux_basis)):
            
            aux_basis_now = aux_basis[i]
            grid_ID = mydf.partition_group_to_gridID[i]
            assert aux_basis_now.shape[1] == grid_ID.size 
            # ngrid_now += grid_ID.size
            # print("i = ", i)
            # print("shift_row = ", shift_row) 
            # print("aux_bas_now = ", aux_basis_now.shape)
            # print("ngrid_now = ", ngrid_now)
            # print("buf = ", buf.shape)
            # print("ngrid_ordering = ", grid_ordering.size)
            # sys.stdout.flush()
        
            fn(mesh_int32.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(aux_basis_now.shape[0]),
                ctypes.c_int(aux_basis_now.shape[1]),
                grid_ID.ctypes.data_as(ctypes.c_void_p),
                aux_basis_now.ctypes.data_as(ctypes.c_void_p),
                coulG_real.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(shift_row),
                V.ctypes.data_as(ctypes.c_void_p),
                grid_ordering.ctypes.data_as(ctypes.c_void_p),
                buf.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(bufsize_per_thread))
        
            shift_row += aux_basis_now.shape[0]

        return V

    if mydf.with_robust_fitting:
        V = construct_V_CCode(mydf.aux_basis, V=None, shift_row=None)
    else:
        V = None
    mydf.V_R = V
    
    ########### construct W ###########
    
    naux_bra = 0
    for x in mydf.aux_basis:
        naux_bra += x.shape[0]
    
    naux = mydf.naux
    
    assert naux % naux_bra == 0
    assert naux // naux_bra == np.prod(mydf.kmesh)
    
    mydf.W = np.zeros((naux_bra, naux), dtype=np.double)
    
    ngroup = len(mydf.aux_basis)    
    aux_bra_shift = 0
    kmesh = mydf.kmesh
        
    for i in range(ngroup):
            
        aux_ket_shift = 0
        grid_shift = 0
        naux_bra = mydf.aux_basis[i].shape[0]
        
        if mydf.with_robust_fitting == False:
            V = construct_V_CCode([mydf.aux_basis[i]], V=None, shift_row=None)
            
        for ix in range(kmesh[0]):
            for iy in range(kmesh[1]):
                for iz in range(kmesh[2]):
                   for j in range(ngroup):
                        aux_basis_ket = mydf.aux_basis[j]
                        ngrid_now = aux_basis_ket.shape[1]
                        naux_ket = aux_basis_ket.shape[0]
                        if mydf.with_robust_fitting:
                            mydf.W[aux_bra_shift:aux_bra_shift+naux_bra, aux_ket_shift:aux_ket_shift+naux_ket] = lib.ddot(
                               V[aux_bra_shift:aux_bra_shift+naux_bra, grid_shift:grid_shift+ngrid_now],
                               aux_basis_ket.T
                            )
                        else:
                            mydf.W[aux_bra_shift:aux_bra_shift+naux_bra, aux_ket_shift:aux_ket_shift+naux_ket] = lib.ddot(
                               V[:, grid_shift:grid_shift+ngrid_now],
                               aux_basis_ket.T
                            )
                        aux_ket_shift += naux_ket
                        grid_shift += ngrid_now                 
                     
        aux_bra_shift += naux_bra
                        
        assert grid_shift == np.prod(mesh)
            
    del buf
    buf = None
    
##### get_jk #####
    
class PBC_ISDF_Info_Quad_K(ISDF_LinearScaling.PBC_ISDF_Info_Quad):
    
    # Quad stands for quadratic scaling
    
    def __init__(self, mol:Cell, 
                 # aoR: np.ndarray = None,
                 with_robust_fitting=True,
                 Ls=None,
                 # get_partition=True,
                 verbose = 1,
                 rela_cutoff_QRCP = None,
                 aoR_cutoff = 1e-8,
                 direct=False
                 ):
        
        super().__init__(mol, with_robust_fitting, None, verbose, rela_cutoff_QRCP, aoR_cutoff, direct)
        
        self.Ls    = Ls
        self.kmesh = Ls
        
        assert self.mesh[0] % Ls[0] == 0
        assert self.mesh[1] % Ls[1] == 0
        assert self.mesh[2] % Ls[2] == 0
        
        #### information relating primitive cell and supercell
        
        self.meshPrim = np.array(self.mesh) // np.array(self.Ls)
        self.natm     = self.cell.natm
        self.natmPrim = self.cell.natm // np.prod(self.Ls)
        
        self.with_translation_symmetry = True
        
        self.primCell = ISDF_K.build_primitive_cell(self.cell, self.kmesh)
        self.nao_prim = self.nao // np.prod(self.kmesh)
        assert self.nao_prim == self.primCell.nao_nr()
        # self.meshPrim = self.mesh // np.array(self.kmesh)
        
        # self.primCell.print()
    
    def build_partition_aoR(self, Ls=None):
        
        if self.aoR is not None and self.partition is not None:
            return
        
        ##### build cutoff info #####   
        
        self.distance_matrix = ISDF_LinearScalingBase.get_cell_distance_matrix(self.cell)
        weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        precision = self.aoR_cutoff
        rcut = ISDF_LinearScalingBase._estimate_rcut(self.cell, self.coords.shape[0], precision)
        rcut_max = np.max(rcut)
        atm2_bas = ISDF_LinearScalingBase._atm_to_bas(self.cell)
        self.AtmConnectionInfo = []
        
        for i in range(self.cell.natm):
            tmp = ISDF_LinearScalingBase.AtmConnectionInfo(self.cell, i, self.distance_matrix, precision, rcut, rcut_max, atm2_bas)
            self.AtmConnectionInfo.append(tmp)
    
        #### information dealing grids , build parition ####
        
        # if Ls is None:
        #     lattice_x = self.cell.lattice_vectors()[0][0]
        #     lattice_y = self.cell.lattice_vectors()[1][1]
        #     lattice_z = self.cell.lattice_vectors()[2][2]
        #     Ls = [int(lattice_x/3)+1, int(lattice_y/3)+1, int(lattice_z/3)+1]
        
        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        # print("self.coords = ", self.coords) 
        
        if Ls is None:
            Ls = [
                int(self.cell.lattice_vectors()[0][0]/2)+1,
                int(self.cell.lattice_vectors()[1][1]/2)+1,
                int(self.cell.lattice_vectors()[2][2]/2)+1
            ]
        
        self.partition_prim = ISDF_LinearScalingBase.get_partition(
            self.cell, self.coords,
            self.AtmConnectionInfo,
            Ls,
            self.with_translation_symmetry, 
            self.kmesh,
            self.use_mpi
        )
        
        for i in range(len(self.partition_prim)):
            self.partition_prim[i] = np.array(self.partition_prim[i], dtype=np.int32)
        
        assert len(self.partition_prim) == self.natmPrim ## the grid id is the global grid id 
        
        self.partition = _expand_partition_prim(self.partition_prim, self.kmesh, self.mesh)
        # print("partition = ", self.partition)
        
        assert len(self.partition) == self.natm
        
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        #### 
        
        if self.verbose:
            _benchmark_time(t1, t2, "build_partition")
        
        
        #### build aoR #### 
        
        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        sync_aoR = False
        if self.direct:
            sync_aoR = True
        
        ## deal with translation symmetry ##
        first_natm = self._get_first_natm()
        natm = self.cell.natm
        #################################### 
        
        sync_aoR = False
        if self.direct:
            sync_aoR = True
        
        ### we need three types of aoR ### 
        
        # this type of aoR is used in get J and select IP 
        
        weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        
        self.aoR = ISDF_LinearScalingBase.get_aoR(self.cell, self.coords, self.partition, 
                                                  first_natm,
                                                  natm,
                                                  self.group_global,
                                                  self.distance_matrix, 
                                                  self.AtmConnectionInfo, 
                                                  self.use_mpi, self.use_mpi, sync_aoR)
        
    
        memory = ISDF_LinearScalingBase._get_aoR_holders_memory(self.aoR)
        
        assert len(self.aoR) == first_natm
        
        if rank == 0:
            print("aoR memory: ", memory) 
        
        weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        
        self.aoR1 = ISDF_LinearScalingBase.get_aoR(self.cell, self.coords, self.partition, 
                                                   None,
                                                   first_natm,
                                                   self.group_global,
                                                   self.distance_matrix, 
                                                   self.AtmConnectionInfo, 
                                                   self.use_mpi, self.use_mpi, sync_aoR)
        
        assert len(self.aoR1) == natm
        
        partition_activated = None
        
        if rank == 0:
            partition_activated = []
            for _id_, aoR_holder in enumerate(self.aoR1):
                if aoR_holder.ao_involved.size == 0:
                    partition_activated.append(False)
                else:
                    partition_activated.append(True)
            partition_activated = np.array(partition_activated, dtype=bool)
        
        if self.use_mpi:
            partition_activated = bcast(partition_activated)
        
        self.partition_activated = partition_activated
        
        self.partition_activated_id = []
        for i in range(len(partition_activated)):
            if partition_activated[i]:
                self.partition_activated_id.append(i)
        self.partition_activated_id = np.array(self.partition_activated_id, dtype=np.int32)
        
        # partition_tmp = []
        # for i in range(len(partition_activated)):
        #     if partition_activated[i]:
        #         partition_tmp.append(self.partition[i])
        #     else:
        #         partition_tmp.append([])
        
        # self.aoR2 = ISDF_LinearScalingBase.get_aoR2(self.cell, self.coords, partition_tmp, 
        #                                             natm,
        #                                             self.group,
        #                                             self.distance_matrix, 
        #                                             self.AtmConnectionInfo, 
        #                                             self.use_mpi, self.use_mpi, sync_aoR)
    
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        if self.verbose:
            _benchmark_time(t1, t2, "build_aoR")
    
    def build_IP_local(self, c=5, m=5, first_natm=None, group=None, Ls = None, debug=True):
        
        first_natm = self._get_first_natm() 
        if group is None:
            group = []
            for i in range(first_natm):
                group.append([i])
        
        ## check the group ##
        
        natm_involved = 0
        for data in group:
            for atm_id in data:
                assert atm_id < first_natm
            natm_involved += len(data)
        assert natm_involved == first_natm 
    
        for i in range(len(group)):
            group[i] = np.array(group[i], dtype=np.int32)
        
        assert len(group) <= first_natm
        
        # build partition and aoR # 
        
        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        self.group = group
        
        self.group_global = []
        shift = 0
        self.atm_ordering = []
        for ix in range(self.kmesh[0]):
            for iy in range(self.kmesh[1]):
                for iz in range(self.kmesh[2]):
                    for data in self.group:
                        self.group_global.append(data + shift)
                        self.atm_ordering.extend(data + shift)
                    shift += self.natmPrim
        self.atm_ordering = np.array(self.atm_ordering, dtype=np.int32)
        print("atm_ordering = ", self.atm_ordering)
                
        self.atm_id_2_group = np.zeros((self.cell.natm), dtype=np.int32)
        for i in range(len(self.group_global)):
            for atm_id in self.group_global[i]:
                self.atm_id_2_group[atm_id] = i
        
        self.build_partition_aoR(None)
        
        self.grid_segment = [0]
        for atm_id in self.atm_ordering:
            # print("self.partition[atm_id] = ", self.partition[atm_id])
            loc_now = self.grid_segment[-1] + len(self.partition[atm_id])
            self.grid_segment.append(loc_now)
            # self.grid_segment.append(self.grid_segment[-1] + len(self.partition[atm_id]))
        self.grid_segment = np.array(self.grid_segment, dtype=np.int32)
        print("grid_segment = ", self.grid_segment)
        
        ao2atomID = self.ao2atomID
        partition = self.partition
        aoR  = self.aoR
        natm = self.natm
        nao  = self.nao
        
        self.partition_atmID_to_gridID = partition
        
        self.partition_group_to_gridID = []
        for i in range(len(group)):
            self.partition_group_to_gridID.append([])
            for atm_id in group[i]:
                self.partition_group_to_gridID[i].extend(partition[atm_id])
            self.partition_group_to_gridID[i] = np.array(self.partition_group_to_gridID[i], dtype=np.int32)
            # self.partition_group_to_gridID[i].sort()
            
        ngrids = self.coords.shape[0]
        
        gridID_2_atmID = np.zeros((ngrids), dtype=np.int32)
        
        for atm_id in range(self.cell.natm):
            gridID_2_atmID[partition[atm_id]] = atm_id
        
        self.gridID_2_atmID = gridID_2_atmID
        self.grid_ID_ordered = _get_grid_ordering_k(self.partition_group_to_gridID, self.kmesh, self.mesh)
        
        self.grid_ID_ordered_prim = self.grid_ID_ordered[:ngrids//np.prod(self.kmesh)].copy()
        
        self.partition_group_to_gridID = _expand_partition_prim(self.partition_group_to_gridID, self.kmesh, self.mesh)
        
        for i in range(len(self.grid_ID_ordered_prim)):
            grid_ID = self.grid_ID_ordered_prim[i]
            
            ix = grid_ID // (self.mesh[1] * self.mesh[2])
            iy = (grid_ID % (self.mesh[1] * self.mesh[2])) // self.mesh[2]
            iz = grid_ID % self.mesh[2]
            
            # assert ix < self.meshPrim[0]
            # assert iy < self.meshPrim[1]
            # assert iz < self.meshPrim[2]
            
            self.grid_ID_ordered_prim[i] = ix * self.meshPrim[1] * self.meshPrim[2] + iy * self.meshPrim[2] + iz
            
        
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        if self.verbose and debug:
            _benchmark_time(t1, t2, "build_partition_aoR")
        
        t1 = t2 
        
        if len(group) < first_natm:
            IP_Atm = ISDF_LinearScaling.select_IP_atm_ls(
                self, c+1, m, first_natm, 
                rela_cutoff=self.rela_cutoff_QRCP,
                no_retriction_on_nIP=self.no_restriction_on_nIP,
                use_mpi=self.use_mpi
            )
        else:
            IP_Atm = ISDF_LinearScaling.select_IP_atm_ls(
                self, c, m, first_natm, 
                rela_cutoff=self.rela_cutoff_QRCP,
                no_retriction_on_nIP=self.no_restriction_on_nIP,
                use_mpi=self.use_mpi
            )
        
        t3 = (lib.logger.process_clock(), lib.logger.perf_counter()) 
        
        weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        
        self.aoRg_possible = ISDF_LinearScalingBase.get_aoR(
            self.cell, self.coords, 
            IP_Atm, 
            first_natm,
            natm,
            self.group,
            self.distance_matrix, 
            self.AtmConnectionInfo, 
            self.use_mpi, self.use_mpi, True
        )
        
        assert len(self.aoRg_possible) == first_natm
        
        t4 = (lib.logger.process_clock(), lib.logger.perf_counter())
        if self.verbose and debug:
            _benchmark_time(t3, t4, "build_aoRg_possible")
        
        select_IP_local_ls_k_drive(
            self, c, m, IP_Atm, group, use_mpi=self.use_mpi
        )
        
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        if self.verbose and debug:
            _benchmark_time(t1, t2, "select_IP")
        
        t1 = t2 
        
        ISDF_LinearScaling.build_aux_basis_ls(
            self, group, self.IP_group, debug=debug, use_mpi=self.use_mpi
        )
        
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        
        if self.verbose and debug:
            _benchmark_time(t1, t2, "build_aux_basis")
    
        t1 = t2
        
        self.aoR_Full = []
        self.aoRg_FUll = []
        
        for i in range(self.kmesh[0]):
            for j in range(self.kmesh[1]):
                for k in range(self.kmesh[2]):
                    self.aoR_Full.append(self._get_aoR_Row(i, j, k))
                    self.aoRg_FUll.append(self._get_aoRg_Row(i, j, k))
        
        
        sys.stdout.flush()
    
    def build_auxiliary_Coulomb(self, debug=True):
        if self.direct == False:
            build_auxiliary_Coulomb_local_bas_k(self, debug=debug, use_mpi=self.use_mpi)

    ##### all the following functions are used to deal with translation symmetry when getting j and getting k #####
    
    def _get_permutation_column_aoR(self, box_x, box_y, box_z, loc_internal=None):
        
        assert box_x < self.kmesh[0]
        assert box_y < self.kmesh[1]
        assert box_z < self.kmesh[2]
        
        if hasattr(self, "aoR_col_permutation") is False:
            self.aoR_col_permutation = []
            for i in range(np.prod(self.kmesh)):
                self.aoR_col_permutation.append(None)
        
        loc = box_x * self.kmesh[1] * self.kmesh[2] + box_y * self.kmesh[2] + box_z 
        
        if self.aoR_col_permutation[loc] is None:
            ### construct the permutation matrix ###
            permutation = []
            for aoR_holder in self.aoR:
                ao_involved = aoR_holder.ao_involved
                ao_permutated = []
                for ao_id in ao_involved:
                    box_id = ao_id // self.nao_prim
                    nao_id = ao_id % self.nao_prim
                    box_x_ = box_id // (self.kmesh[1] * self.kmesh[2])
                    box_y_ = (box_id % (self.kmesh[1] * self.kmesh[2])) // self.kmesh[2]
                    box_z_ = box_id % self.kmesh[2]
                    box_x_new = (box_x + box_x_) % self.kmesh[0]
                    box_y_new = (box_y + box_y_) % self.kmesh[1]
                    box_z_new = (box_z + box_z_) % self.kmesh[2]
                    nao_id_new = box_x_new * self.kmesh[1] * self.kmesh[2] * self.nao_prim + box_y_new * self.kmesh[2] * self.nao_prim + box_z_new * self.nao_prim + nao_id
                    ao_permutated.append(nao_id_new)
                # print("ao_permutated = ", ao_permutated)
                permutation.append(np.array(ao_permutated, dtype=np.int32))
            self.aoR_col_permutation[loc] = permutation
        
        if loc_internal is not None:
            return self.aoR_col_permutation[loc][loc_internal]
        else:
            return self.aoR_col_permutation[loc]
    
    def _get_permutation_column_aoRg(self, box_x, box_y, box_z, loc_internal=None):
        
        assert box_x < self.kmesh[0]
        assert box_y < self.kmesh[1]
        assert box_z < self.kmesh[2]
    
        if hasattr(self, "aoRg_col_permutation") is False:
            self.aoRg_col_permutation = []
            for i in range(np.prod(self.kmesh)):
                self.aoRg_col_permutation.append(None)
        
        loc = box_x * self.kmesh[1] * self.kmesh[2] + box_y * self.kmesh[2] + box_z
        
        if self.aoRg_col_permutation[loc] is None:
            ### construct the permutation matrix ###
            permutation = []
            for aoRg_holder in self.aoRg:
                ao_involved = aoRg_holder.ao_involved
                ao_permutated = []
                for ao_id in ao_involved:
                    box_id = ao_id // self.nao_prim
                    nao_id = ao_id % self.nao_prim
                    box_x_ = box_id // (self.kmesh[1] * self.kmesh[2])
                    box_y_ = (box_id % (self.kmesh[1] * self.kmesh[2])) // self.kmesh[2]
                    box_z_ = box_id % self.kmesh[2]
                    box_x_new = (box_x + box_x_) % self.kmesh[0]
                    box_y_new = (box_y + box_y_) % self.kmesh[1]
                    box_z_new = (box_z + box_z_) % self.kmesh[2]
                    nao_id_new = box_x_new * self.kmesh[1] * self.kmesh[2] * self.nao_prim + box_y_new * self.kmesh[2] * self.nao_prim + box_z_new * self.nao_prim + nao_id
                    ao_permutated.append(nao_id_new)
                permutation.append(np.array(ao_permutated, dtype=np.int32))
            self.aoRg_col_permutation[loc] = permutation
        
        if loc_internal is not None:
            return self.aoRg_col_permutation[loc][loc_internal]
        else:
            return self.aoRg_col_permutation[loc]
    
    def get_aoR_Row(self, box_x, box_y, box_z):
        loc = box_x * self.kmesh[1] * self.kmesh[2] + box_y * self.kmesh[2] + box_z
        return self.aoR_Full[loc]

    def get_aoRg_Row(self, box_x, box_y, box_z):
        loc = box_x * self.kmesh[1] * self.kmesh[2] + box_y * self.kmesh[2] + box_z
        return self.aoRg_FUll[loc]
    
    def _get_aoR_Row(self, box_x, box_y, box_z):
        
        assert box_x < self.kmesh[0]
        assert box_y < self.kmesh[1]
        assert box_z < self.kmesh[2]
        
        if box_x == 0 and box_y == 0 and box_z == 0:
            return self.aoR1
        else:
            Res = []
            for ix in range(self.kmesh[0]):
                for iy in range(self.kmesh[1]):
                    for iz in range(self.kmesh[2]):
                        ix_ = (ix - box_x + self.kmesh[0]) % self.kmesh[0]
                        iy_ = (iy - box_y + self.kmesh[1]) % self.kmesh[1]
                        iz_ = (iz - box_z + self.kmesh[2]) % self.kmesh[2]
                        loc_ = ix_ * self.kmesh[1] * self.kmesh[2] + iy_ * self.kmesh[2] + iz_
                        for i in range(loc_*self.natmPrim, (loc_+1)*self.natmPrim):
                            Res.append(self.aoR1[i])
            return Res
    
    def _get_aoRg_Row(self, box_x, box_y, box_z):
        assert box_x < self.kmesh[0]
        assert box_y < self.kmesh[1]
        assert box_z < self.kmesh[2]
        
        if box_x == 0 and box_y == 0 and box_z == 0:
            return self.aoRg1
        else:
            Res = []
            for ix in range(self.kmesh[0]):
                for iy in range(self.kmesh[1]):
                    for iz in range(self.kmesh[2]):
                        ix_ = (ix - box_x + self.kmesh[0]) % self.kmesh[0]
                        iy_ = (iy - box_y + self.kmesh[1]) % self.kmesh[1]
                        iz_ = (iz - box_z + self.kmesh[2]) % self.kmesh[2]
                        loc_ = ix_ * self.kmesh[1] * self.kmesh[2] + iy_ * self.kmesh[2] + iz_
                        for i in range(loc_*self.natmPrim, (loc_+1)*self.natmPrim):
                            Res.append(self.aoRg1[i])
            return Res

    def _construct_RgAO(self, dm, aoRg_holders):
        
        fn_packrow = getattr(libpbc, "_buildK_packrow", None)
        assert fn_packrow is not None
        fn_packcol = getattr(libpbc, "_buildK_packcol", None)
        assert fn_packcol is not None
    
        # if hasattr(self, "dm_reorder_buf") is False:
        #     self.dm_reorder_buf = np.zeros((self.nao_prim, self.nao), dtype=np.double)
        
        naux_involved_tot = 0
        naux_involved_max = 0
        nao_involved_max = 0
        for data, _ in aoRg_holders:
            naux_involved_tot += data.aoR.shape[1]
            naux_involved_max = max(naux_involved_max, data.aoR.shape[1])
            nao_involved_max = max(nao_involved_max, data.ao_involved.size)

        if hasattr(self, "dm_pack_buf") is False:
            self.dm_pack_buf = np.zeros((nao_involved_max, self.nao), dtype=np.double)
        else:
            if self.dm_pack_buf.shape[0] < nao_involved_max:
                self.dm_pack_buf = np.zeros((nao_involved_max, self.nao), dtype=np.double)
        
        if hasattr(self, "RgAO_ddot_buf") is False:
            self.RgAO_ddot_buf = np.zeros((naux_involved_max, self.nao), dtype=np.double)
        else:
            if self.RgAO_ddot_buf.shape[0] < naux_involved_max:
                self.RgAO_ddot_buf = np.zeros((naux_involved_max, self.nao), dtype=np.double)
        
        if hasattr(self, "RgAO") is False:
            self.RgAO = np.zeros((naux_involved_tot, self.nao), dtype=np.double)
        else:
            if self.RgAO.shape[0] < naux_involved_tot:
                self.RgAO = np.zeros((naux_involved_tot, self.nao), dtype=np.double)
        
        grid_loc = 0
        
        
        res = np.ndarray((naux_involved_tot, self.nao), buffer=self.RgAO)
        
        for aoR_holder, permutation in aoRg_holders:
            
            ngrid_now = aoR_holder.aoR.shape[1]
            nao_invovled = aoR_holder.ao_involved.size
            
            dm_packed = np.ndarray((nao_invovled, self.nao), buffer=self.dm_pack_buf)
            
            fn_packrow(
                dm_packed.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(nao_invovled),
                ctypes.c_int(self.nao),
                dm.ctypes.data_as(ctypes.c_void_p),
                ctypes.c_int(self.nao), # TODO: optimized 
                ctypes.c_int(self.nao), # TODO: optimized 
                permutation.ctypes.data_as(ctypes.c_void_p),
            )
            
            ddot_res = np.ndarray((ngrid_now, self.nao), buffer=self.RgAO_ddot_buf)
            lib.ddot(aoR_holder.aoR.T, dm_packed, c=ddot_res)
            res[grid_loc:grid_loc+ngrid_now,:] = ddot_res
            
            # dm_tmp = dm[permutation, :]
            # benchmark = lib.ddot(aoR_holder.aoR.T, dm_tmp)
            # diff = benchmark - ddot_res
            # print("_construct_RgAO diff = ", np.linalg.norm(diff)/np.sqrt(ddot_res.size))
            
            grid_loc += ngrid_now
        
        return res

    def _construct_RgR(self, RgAO, construct_RgRg=False):

        naux = RgAO.shape[0]
        ngrid = np.prod(self.mesh)
        
        if hasattr(self, "RgR") is False:
            self.RgR = np.zeros((naux, ngrid), dtype=np.double)
        else:
            if self.RgR.shape[0] < naux:
                self.RgR = np.zeros((naux, ngrid), dtype=np.double)

        if hasattr(self, "RgAO_pack_buf") is False:
            max_nao_involved = np.max([x.ao_involved.size for x in self.aoR1 if x is not None])
            self.RgAO_pack_buf = np.zeros((naux, max_nao_involved), dtype=np.double)
        else:
            if self.RgAO_pack_buf.shape[0] < naux:
                self.RgAO_pack_buf = np.zeros((naux, self.RgAO_pack_buf.shape[1]), dtype=np.double)

        if hasattr(self, "ddot_res_RgR_buf") is False:
            max_ngrid_invovled = np.max([x.aoR.shape[1] for x in self.aoR1 if x is not None])
            self.ddot_res_RgR_buf = np.zeros((naux, max_ngrid_invovled), dtype=np.double)
        else:
            if self.ddot_res_RgR_buf.shape[0] < naux:
                self.ddot_res_RgR_buf = np.zeros((naux, self.ddot_res_RgR_buf.shape[1]), dtype=np.double)

        if construct_RgRg:
            Res = np.ndarray((naux, self.naux), buffer=self.RgR)
        else:
            Res = np.ndarray((naux, ngrid), buffer=self.RgR)
            
        Res.ravel()[:] = 0.0

        kmesh = self.kmesh

        fn_packcol1 = getattr(libpbc, "_buildK_packcol", None)
        assert fn_packcol1 is not None

        loc = 0
        for ix in range(kmesh[0]):
            for iy in range(kmesh[1]):
                for iz in range(kmesh[2]):
                    
                    if construct_RgRg:
                        aoR_now = self.get_aoRg_Row(ix, iy, iz)
                    else:
                        aoR_now = self.get_aoR_Row(ix, iy, iz)
                    
                    RgAO_packed = RgAO[:, loc*self.nao_prim:(loc+1)*self.nao_prim].copy()

                    # for _loc_, aoR_holder in enumerate(aoR_now):
                    
                    for _loc_ in self.atm_ordering:
                        
                        aoR_holder = aoR_now[_loc_]
                        
                        if aoR_holder is None:
                            continue # achieve linear scaling here
                    
                        aoR = aoR_holder.aoR
                        ao_involved = aoR_holder.ao_involved
                        nao_involved = ao_involved.size
                        
                        ngrid_now = aoR.shape[1]
                        if construct_RgRg:
                            grid_begin = self.IP_segment[_loc_]
                            assert grid_begin + ngrid_now == self.IP_segment[_loc_+1]
                        else:
                            grid_begin = self.grid_segment[_loc_]
                            assert grid_begin + ngrid_now == self.grid_segment[_loc_+1]

                        if nao_involved == self.nao_prim:
                            Density_RgAO_packed = RgAO_packed
                        else:
                            Density_RgAO_packed = np.ndarray((naux, nao_involved), buffer=self.RgAO_pack_buf)
                            fn_packcol1(
                                Density_RgAO_packed.ctypes.data_as(ctypes.c_void_p),
                                ctypes.c_int(naux),
                                ctypes.c_int(nao_involved),
                                RgAO_packed.ctypes.data_as(ctypes.c_void_p),
                                ctypes.c_int(naux),
                                ctypes.c_int(self.nao_prim),
                                ao_involved.ctypes.data_as(ctypes.c_void_p)
                            )

                        ddot_res = np.ndarray((naux, ngrid_now), buffer=self.ddot_res_RgR_buf)
                        lib.ddot(Density_RgAO_packed, aoR, c=ddot_res)
                        # print("grid_begin = ", grid_begin, "ngrid_now = ", ngrid_now)
                        Res[:, grid_begin:grid_begin+ngrid_now] += ddot_res

                    loc += 1

        ## only for debug ## 
        
        # if construct_RgRg is False:
        #     weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        #     aoR_benchmark = ISDF_eval_gto(self.cell, coords=self.coords[self.grid_ID_ordered]) * weight
        #     res_bench = lib.ddot(RgAO, aoR_benchmark)
        #     diff = res_bench - Res
        #     print("_construct_RgR False diff = ", np.linalg.norm(diff)/np.sqrt(Res.size))
        # else:
        #     weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        #     aoRg_benchmark = ISDF_eval_gto(self.cell, coords=self.coords[self.IP_flat]) * weight
        #     res_bench = lib.ddot(RgAO, aoRg_benchmark)
        #     diff = res_bench - Res
        #     print("_construct_RgR True  diff = ", np.linalg.norm(diff)/np.sqrt(Res.size))

        return Res

    def _construct_K1_tmp1(self, V2, construct_K2_W=False):
        
        naux = V2.shape[0]
        nao = self.nao
        nao_prim = self.nao_prim
        # ngrid = np.prod(self.mesh)
        ngrid = V2.shape[1]
        
        if hasattr(self, "K1_tmp1_buf") is False:
            self.K1_tmp1_buf = np.zeros((naux, nao), dtype=np.double)
            self.K1_tmp1_subres_buf = np.zeros((naux, nao_prim), dtype=np.double)
            self.K1_ddot_res_buf = np.zeros((naux, nao_prim), dtype=np.double)
            
            max_ngrid_involved = np.max([x.aoR.shape[1] for x in self.aoR1 if x is not None])
            self.K1_tmp1_V_pack_buf = np.zeros((naux, max_ngrid_involved), dtype=np.double)
            
        else:
            if self.K1_tmp1_buf.shape[0] < naux:
                self.K1_tmp1_buf = np.zeros((naux, nao), dtype=np.double)
                self.K1_tmp1_subres_buf = np.zeros((naux, nao_prim), dtype=np.double)
                self.K1_ddot_res_buf = np.zeros((naux, nao_prim), dtype=np.double)
                self.K1_tmp1_V_pack_buf = np.zeros((naux, self.K1_tmp1_V_pack_buf.shape[1]), dtype=np.double)
        
        K1_tmp1 = np.ndarray((naux, nao), buffer=self.K1_tmp1_buf)
        
        # return K1_tmp1
        
        K1_tmp1_subres = np.ndarray((naux, nao_prim), buffer=self.K1_tmp1_subres_buf)
        
        fn_packcol2 = getattr(libpbc, "_buildK_packcol2", None)
        assert fn_packcol2 is not None
        fn_packadd_col = getattr(libpbc, "_buildK_packaddcol", None)
        assert fn_packadd_col is not None
    
        # K1_tmp1.ravel()[:] = 0.0
        
        # if construct_K2_W:
        #     weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        #     aoRg_tmp = ISDF_eval_gto(self.cell, coords=self.coords[self.IP_flat]) * weight
        #     benchmark = lib.ddot(V2, aoRg_tmp.T)
        # else:
        #     weight = np.sqrt(self.cell.vol / self.coords.shape[0])
        #     aoR_tmp = ISDF_eval_gto(self.cell, coords=self.coords[self.grid_ID_ordered]) * weight
        #     benchmark = lib.ddot(V2, aoR_tmp.T)
        
        loc = 0
        
        for ix in range(self.kmesh[0]):
            for iy in range(self.kmesh[1]):
                for iz in range(self.kmesh[2]):
                    
                    if construct_K2_W:
                        aoR_holders = self.get_aoRg_Row(ix, iy, iz)
                    else:
                        aoR_holders = self.get_aoR_Row(ix, iy, iz)
                    
                    K1_tmp1_subres.ravel()[:] = 0.0
                    
                    # grid_loc = 0
                    
                    for _loc_ in self.atm_ordering:
                        
                        aoR_holder = aoR_holders[_loc_]
                        
                        if aoR_holder is None:
                            continue
                        
                        if construct_K2_W:
                            grid_loc = self.IP_segment[_loc_]
                        else:
                            grid_loc = self.grid_segment[_loc_]
                    
                        ngrid_now = aoR_holder.aoR.shape[1]
                        nao_involved = aoR_holder.ao_involved.size
                        
                        ddot_res = np.ndarray((naux, nao_involved), buffer=self.K1_ddot_res_buf)
                        
                        V_packed = np.ndarray((naux, ngrid_now), buffer=self.K1_tmp1_V_pack_buf)
                        
                        fn_packcol2(
                            V_packed.ctypes.data_as(ctypes.c_void_p),
                            ctypes.c_int(naux),
                            ctypes.c_int(ngrid_now),
                            V2.ctypes.data_as(ctypes.c_void_p),
                            ctypes.c_int(naux),
                            ctypes.c_int(ngrid),
                            ctypes.c_int(grid_loc),
                            ctypes.c_int(grid_loc+ngrid_now)
                        )
                        
                        lib.ddot(V_packed, aoR_holder.aoR.T, c=ddot_res)
                        
                        if nao_involved == nao_prim:
                            K1_tmp1_subres += ddot_res
                        else:
                            fn_packadd_col(
                                K1_tmp1_subres.ctypes.data_as(ctypes.c_void_p),
                                ctypes.c_int(naux),
                                ctypes.c_int(nao_prim),
                                ddot_res.ctypes.data_as(ctypes.c_void_p),
                                ctypes.c_int(naux),
                                ctypes.c_int(nao_involved),
                                aoR_holder.ao_involved.ctypes.data_as(ctypes.c_void_p)
                            )
                        
                        # grid_loc += ngrid_now
                    
                    
                    K1_tmp1[:, loc * nao_prim:(loc+1) * nao_prim] = K1_tmp1_subres
                    loc += 1
                    
        # assert loc == np.prod(self.kmesh)
        
        # print("diff = ", np.linalg.norm(K1_tmp1.ravel() - benchmark.ravel())/np.sqrt(benchmark.size))
        # assert np.allclose(benchmark, K1_tmp1)
        
        return K1_tmp1
       
    def _permutate_K1_tmp1(self, K_tmp1, box_id):
        
        box_x = box_id // (self.kmesh[1] * self.kmesh[2])
        box_y = (box_id % (self.kmesh[1] * self.kmesh[2])) // self.kmesh[2]
        box_z = box_id % self.kmesh[2]
        
        if hasattr(self, "K_tmp1_permutation_buf") is False:
            self.K_tmp1_permutation_buf = np.zeros_like(K_tmp1)
        else:
            if self.K_tmp1_permutation_buf.shape[0] < K_tmp1.shape[0]:
                self.K_tmp1_permutation_buf = np.zeros_like(K_tmp1)
        
        K_tmp1_permutation = np.ndarray(K_tmp1.shape, buffer=self.K_tmp1_permutation_buf)

        loc = 0
        for i in range(self.kmesh[0]):
            for j in range(self.kmesh[1]):
                for k in range(self.kmesh[2]):
                    ix_ = (i - box_x + self.kmesh[0]) % self.kmesh[0]
                    iy_ = (j - box_y + self.kmesh[1]) % self.kmesh[1]
                    iz_ = (k - box_z + self.kmesh[2]) % self.kmesh[2]
                    loc_ = ix_ * self.kmesh[1] * self.kmesh[2] + iy_ * self.kmesh[2] + iz_
                    K_tmp1_permutation[:, loc*self.nao_prim:(loc+1)*self.nao_prim] = K_tmp1[:, loc_*self.nao_prim:(loc_+1)*self.nao_prim]
                    loc += 1    
    
    
        return K_tmp1_permutation
        
        
        
       
    def _construct_W_tmp(self, V_tmp, Res):
        
        assert V_tmp.shape[0] == Res.shape[0]
        assert Res.shape[1] == self.naux
        
        # Res.ravel()[:] = 0.0
        # return Res
        
        grid_loc = 0 
        aux_col_loc = 0
        for ix in range(self.kmesh[0]):
            for iy in range(self.kmesh[1]):
                for iz in range(self.kmesh[2]):
                    
                    for j in range(len(self.group)):
                        
                        aux_bas_ket = self.aux_basis[j]
                        naux_ket = aux_bas_ket.shape[0]
                        ngrid_now = aux_bas_ket.shape[1]
                        Res[:, aux_col_loc:aux_col_loc+naux_ket] = lib.ddot(V_tmp[:, grid_loc:grid_loc+ngrid_now], aux_bas_ket.T)

                        aux_col_loc += naux_ket
                        grid_loc    += ngrid_now
        
        assert aux_col_loc == self.naux
        assert grid_loc == np.prod(self.mesh)   
        
        return Res
         
    def allocate_k_buffer(self): 
        ### TODO: split grid again to reduce the size of buf when robust fitting is true! 
        # TODO: try to calculate the size when direct is true
        
        max_nao_involved = self._get_max_nao_involved()
        max_ngrid_involved = self._get_max_ngrid_involved()
        max_nIP_involved = self._get_max_nIP_involved()
        maxsize_group_naux = self._get_maxsize_group_naux()
        
        allocated = False
        
        if self.direct:
            if hasattr(self, "build_VW_in_k_buf") and self.build_VW_in_k_buf is not None:
                allocated = True
        else:
            raise NotImplementedError("allocate_k_buffer for robust fitting without direct is not implemented yet!") 
                   
        if allocated:
            pass
        else:
            
            nThread = lib.num_threads()
            bufsize_per_thread = np.prod(self.mesh) * 4
            
            size1 = nThread * bufsize_per_thread 
            size2 = maxsize_group_naux * np.prod(self.mesh)
            size3 = max_nao_involved * self.nao
            size4 = maxsize_group_naux * self.naux
            size3 = max(size3, size4)
            size4 = max_nao_involved * self.nao
            
            self.build_VW_in_k_buf = np.zeros((size1+size2+size3+size4), dtype=np.double)
            
                       
    get_jk = get_jk_dm_k_quadratic

from pyscf.pbc.df.isdf.isdf_k import build_supercell
from pyscf.pbc.df.isdf.isdf_split_grid import build_supercell_with_partition

C = 8

if __name__ == "__main__":
    
    verbose = 4
    if rank != 0:
        verbose = 0
        
    cell   = pbcgto.Cell()
    boxlen = 3.5668
    cell.a = np.array([[boxlen,0.0,0.0],[0.0,boxlen,0.0],[0.0,0.0,boxlen]])
    prim_a = np.array([[boxlen,0.0,0.0],[0.0,boxlen,0.0],[0.0,0.0,boxlen]])
    atm = [
        ['C', (0.     , 0.     , 0.    )],
        ['C', (0.8917 , 0.8917 , 0.8917)],
        ['C', (1.7834 , 1.7834 , 0.    )],
        ['C', (2.6751 , 2.6751 , 0.8917)],
        ['C', (1.7834 , 0.     , 1.7834)],
        ['C', (2.6751 , 0.8917 , 2.6751)],
        ['C', (0.     , 1.7834 , 1.7834)],
        ['C', (0.8917 , 2.6751 , 2.6751)],
    ] 
    
    KE_CUTOFF = 64
    
    prim_cell = build_supercell(atm, prim_a, Ls = [1,1,1], ke_cutoff=KE_CUTOFF)
    prim_mesh = prim_cell.mesh
    prim_partition = [[0], [1], [2], [3], [4], [5], [6], [7]]
    # prim_partition = [[0,1,2,3,4,5,6,7]]
    
    Ls = [1, 3, 3]
    Ls = np.array(Ls, dtype=np.int32)
    mesh = [Ls[0] * prim_mesh[0], Ls[1] * prim_mesh[1], Ls[2] * prim_mesh[2]]
    mesh = np.array(mesh, dtype=np.int32)
    
    cell, group_partition = build_supercell_with_partition(atm, prim_a, mesh=mesh, 
                                                     Ls=Ls,
                                                     #basis=basis, pseudo=pseudo,
                                                     partition=prim_partition, ke_cutoff=KE_CUTOFF, verbose=verbose)
    
    pbc_isdf_info = PBC_ISDF_Info_Quad_K(cell, Ls=Ls, with_robust_fitting=True, aoR_cutoff=1e-8, direct=True, rela_cutoff_QRCP=1e-3)
    pbc_isdf_info.build_IP_local(c=C, m=5, group=prim_partition, Ls=[Ls[0]*10, Ls[1]*10, Ls[2]*10])
    
    # exit(1)
    
    print("grid_segment = ", pbc_isdf_info.grid_segment)
    
    print("len of grid_ordering = ", len(pbc_isdf_info.grid_ID_ordered))
    
    aoR_unpacked = []
    for aoR_holder in pbc_isdf_info.aoR1:
        aoR_unpacked.append(aoR_holder.todense(prim_cell.nao_nr()))
    aoR_unpacked = np.concatenate(aoR_unpacked, axis=1)
    print("aoR_unpacked shape = ", aoR_unpacked.shape)
    
    weight = np.sqrt(cell.vol / pbc_isdf_info.coords.shape[0])
    aoR_benchmark = ISDF_eval_gto(cell, coords=pbc_isdf_info.coords[pbc_isdf_info.grid_ID_ordered]) * weight
    loc = 0
    nao_prim = prim_cell.nao_nr()
    for ix in range(Ls[0]):
        for iy in range(Ls[1]):
            for iz in range(Ls[2]):
                aoR_unpacked = []
                aoR_holder = pbc_isdf_info.get_aoR_Row(ix, iy, iz)
                for data in aoR_holder:
                    # print("data = ", data.aoR.shape)
                    aoR_unpacked.append(data.todense(nao_prim))
                aoR_unpacked = np.concatenate(aoR_unpacked, axis=1)
                aoR_benchmark_now = aoR_benchmark[loc*nao_prim:(loc+1)*nao_prim,:]
                loc += 1
    # aoR_benchmark = aoR_benchmark[:prim_cell.nao_nr(),:]
                diff = aoR_benchmark_now - aoR_unpacked
                where = np.where(np.abs(diff) > 1e-4)
                # print(aoR_benchmark_now[78,:])
                # print(aoR_unpacked[78,:])
                # print("where = ", where)    
                # print(aoR_benchmark_now[0,0], aoR_unpacked[0,0])
                print("diff = ", np.linalg.norm(diff)/np.sqrt(aoR_unpacked.size))
    
    print("prim_mesh = ", prim_mesh)
    
    # exit(1)
    
    naux_prim = 0
    for data in pbc_isdf_info.aoRg:
        naux_prim += data.aoR.shape[1]
    print("naux_prim = ", naux_prim)
    print("naux = ", pbc_isdf_info.naux)
    
    aoR_unpacked = np.zeros_like(aoR_benchmark)
    ngrid = 0
    for ix in range(Ls[0]):
        for iy in range(Ls[1]):
            for iz in range(Ls[2]):
                perm_col = pbc_isdf_info._get_permutation_column_aoR(ix, iy, iz)
                for _loc_, data in enumerate(pbc_isdf_info.aoR):
                    aoR_unpacked[perm_col[_loc_], ngrid:ngrid+data.aoR.shape[1]] = data.aoR
                    ngrid += data.aoR.shape[1]
    assert ngrid == np.prod(mesh)
    diff = aoR_benchmark - aoR_unpacked
    where = np.where(np.abs(diff) > 1e-4)
    print("where = ", where)
    print("diff = ", np.linalg.norm(diff)/np.sqrt(aoR_unpacked.size)) 
    
    ngrid_prim = np.prod(prim_mesh)
    diff = aoR_benchmark[:, :ngrid_prim] - aoR_unpacked[:,:ngrid_prim]
    print("diff.shape = ", diff.shape)
    print("diff = ", np.linalg.norm(diff)/np.sqrt(diff.size))
    where = np.where(np.abs(diff) > 1e-4)
    print("where = ", where)
    
    grid_ID_prim = pbc_isdf_info.grid_ID_ordered[:ngrid_prim]
    grid_ID_prim2 = []
    for i in range(pbc_isdf_info.natmPrim):
        grid_ID_prim2.extend(pbc_isdf_info.partition[i])
    grid_ID_prim2 = np.array(grid_ID_prim2, dtype=np.int32)
    assert np.allclose(grid_ID_prim, grid_ID_prim2)
    
    # exit(1)
    
    pbc_isdf_info.build_auxiliary_Coulomb(debug=True)
    
    print("grid_segment = ", pbc_isdf_info.grid_segment)
    
    from pyscf.pbc import scf

    mf = scf.RHF(cell)
    # mf = scf.addons.smearing_(mf, sigma=0.2, method='fermi')
    pbc_isdf_info.direct_scf = mf.direct_scf
    mf.with_df = pbc_isdf_info
    mf.max_cycle = 16
    mf.conv_tol = 1e-7
    
    mf.kernel()
    
    exit(1)
    
    ######### bench mark #########
    
    pbc_isdf_info = ISDF_LinearScaling.PBC_ISDF_Info_Quad(cell, with_robust_fitting=True, aoR_cutoff=1e-8, direct=True, rela_cutoff_QRCP=3e-3)
    pbc_isdf_info.build_IP_local(c=C, m=5, group=group_partition, Ls=[Ls[0]*10, Ls[1]*10, Ls[2]*10])
    # pbc_isdf_info.build_IP_local(c=C, m=5, group=group_partition, Ls=[Ls[0]*3, Ls[1]*3, Ls[2]*3])
    pbc_isdf_info.Ls = Ls
    pbc_isdf_info.build_auxiliary_Coulomb(debug=True)
    
    aoR_unpacked = []
    for aoR_holder in pbc_isdf_info.aoR:
        aoR_unpacked.append(aoR_holder.todense(cell.nao_nr()))
    aoR_unpacked = np.concatenate(aoR_unpacked, axis=1)
    grid_ordered = pbc_isdf_info.grid_ID_ordered
    aoR_benchmark = ISDF_eval_gto(cell, coords=pbc_isdf_info.coords[grid_ordered]) * weight
    diff = aoR_benchmark - aoR_unpacked
    print("diff = ", np.linalg.norm(diff)/np.sqrt(aoR_unpacked.size))
    exit(1)
    
    mf = scf.RHF(cell)
    pbc_isdf_info.direct_scf = mf.direct_scf
    mf.with_df = pbc_isdf_info
    mf.max_cycle = 16
    mf.conv_tol = 1e-7
    # mf.kernel()