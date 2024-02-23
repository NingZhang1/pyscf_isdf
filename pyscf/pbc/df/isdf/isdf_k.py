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

from pyscf.pbc.df.isdf.isdf_fast import PBC_ISDF_Info

import pyscf.pbc.df.isdf.isdf_outcore as ISDF_outcore
import pyscf.pbc.df.isdf.isdf_fast as ISDF

import ctypes

from multiprocessing import Pool

from memory_profiler import profile

libpbc = lib.load_library('libpbc')

from pyscf.pbc.df.isdf.isdf_eval_gto import ISDF_eval_gto

####################### Util Module #######################

def _extract_grid_primitive_cell(cell_a, mesh, Ls, coords):
    """
    Extract the primitive cell grid information from the supercell grid information
    """
    
    #print("In _extract_grid_primitive_cell")
    
    assert cell_a[0, 1] == 0.0
    assert cell_a[0, 2] == 0.0
    assert cell_a[1, 0] == 0.0
    assert cell_a[1, 2] == 0.0
    assert cell_a[2, 0] == 0.0
    assert cell_a[2, 1] == 0.0
    
    ngrids = np.prod(mesh)
    # print("ngrids = ", ngrids)

    assert ngrids == coords.shape[0]
    
    Lx = Ls[0]
    Ly = Ls[1]
    Lz = Ls[2]
    
    # print("Lx = ", Lx)
    # print("Ly = ", Ly)
    # print("Lz = ", Lz)
    
    print("Length supercell x = %15.6f , primitive cell x = %15.6f" % (cell_a[0, 0], cell_a[0, 0] / Lx))
    print("Length supercell y = %15.6f , primitive cell y = %15.6f" % (cell_a[1, 1], cell_a[1, 1] / Ly))
    print("Length supercell z = %15.6f , primitive cell z = %15.6f" % (cell_a[2, 2], cell_a[2, 2] / Lz))
    
    nx, ny, nz = mesh
    
    # print("nx = ", nx)
    # print("ny = ", ny)
    # print("nz = ", nz)
    
    coords = coords.reshape(nx, ny, nz, 3)
    
    assert nx % Lx == 0
    assert ny % Ly == 0
    assert nz % Lz == 0
    
    nx_prim = nx // Lx
    ny_prim = ny // Ly
    nz_prim = nz // Lz
    
    # print("nx_prim = ", nx_prim)
    # print("ny_prim = ", ny_prim)
    # print("nz_prim = ", nz_prim)
    
    ngrids_prim = nx_prim * ny_prim * nz_prim
    
    res_dict = {}
    
    res = []
        
    prim_grid = coords[:nx_prim, :ny_prim, :nz_prim].reshape(-1, 3)
        
    for ix in range(Lx):
        for iy in range(Ly):
            for iz in range(Lz):
                x_0 = ix * nx_prim
                x_1 = (ix + 1) * nx_prim
                y_0 = iy * ny_prim
                y_1 = (iy + 1) * ny_prim
                z_0 = iz * nz_prim
                z_1 = (iz + 1) * nz_prim
                
                grid_tmp = coords[x_0:x_1, y_0:y_1, z_0:z_1].reshape(-1, 3)
                
                shift_bench = np.zeros((3), dtype=np.float64)
                shift_bench[0] = ix * cell_a[0, 0] / Lx
                shift_bench[1] = iy * cell_a[1, 1] / Ly
                shift_bench[2] = iz * cell_a[2, 2] / Lz
                
                shifts = grid_tmp - prim_grid
                
                # print("shifts = ", shifts)
                # print("shift_bench = ", shift_bench)
                
                for ID in range(shifts.shape[0]):
                    shift = shifts[ID]
                    # print("shift = ", shift)
                    if np.allclose(shift, shift_bench) == False:
                        tmp = shift - shift_bench
                        nx = round (tmp[0] / cell_a[0, 0])
                        ny = round (tmp[1] / cell_a[1, 1])
                        nz = round (tmp[2] / cell_a[2, 2])
                        # print(tmp)
                        # print(nx, ny, nz)
                        assert np.allclose(tmp[0], nx * cell_a[0, 0])
                        assert np.allclose(tmp[1], ny * cell_a[1, 1])
                        assert np.allclose(tmp[2], nz * cell_a[2, 2])
                        # grid_tmp[ID] = prim_grid[ID] + shift_bench, do not shift to avoid numerical error

                res.append(grid_tmp)
                res_dict[(ix, iy, iz)] = grid_tmp
    res = np.array(res).reshape(-1, 3)
    return res, res_dict

def _split_partition(Voroini_partition, mesh, Ls):
    ngrids = np.prod(mesh)
    assert ngrids == coords.shape[0]
    
    Lx = Ls[0]
    Ly = Ls[1]
    Lz = Ls[2]

    nx, ny, nz = mesh
    
    Voroini_partition_reshaped = Voroini_partition.reshape(nx, ny, nz)
        
    assert nx % Lx == 0
    assert ny % Ly == 0
    assert nz % Lz == 0
    
    nx_prim = nx // Lx
    ny_prim = ny // Ly
    nz_prim = nz // Lz
    
    ngrids_prim = nx_prim * ny_prim * nz_prim
    
    res_dict = {}
    prim_grid = Voroini_partition_reshaped[:nx_prim, :ny_prim, :nz_prim].reshape(-1, 3)
        
    for ix in range(Lx):
        for iy in range(Ly):
            for iz in range(Lz):
                x_0 = ix * nx_prim
                x_1 = (ix + 1) * nx_prim
                y_0 = iy * ny_prim
                y_1 = (iy + 1) * ny_prim
                z_0 = iz * nz_prim
                z_1 = (iz + 1) * nz_prim
                
                grid_tmp               = Voroini_partition_reshaped[x_0:x_1, y_0:y_1, z_0:z_1].reshape(-1)
                res_dict[(nx, ny, nz)] = grid_tmp
    
    return res_dict
    
def build_supercell(prim_atm, 
                    prim_a, 
                    mesh=None, 
                    Ls = [1,1,1], 
                    basis='gth-dzvp', 
                    pseudo='gth-pade', 
                    ke_cutoff=70, 
                    max_memory=2000, 
                    precision=1e-8,
                    use_particle_mesh_ewald=True):
    
    Cell = pbcgto.Cell()
    
    assert prim_a[0, 1] == 0.0
    assert prim_a[0, 2] == 0.0
    assert prim_a[1, 0] == 0.0
    assert prim_a[1, 2] == 0.0
    assert prim_a[2, 0] == 0.0
    assert prim_a[2, 1] == 0.0
    
    Supercell_a = prim_a * np.array(Ls)
    Cell.a = Supercell_a
    
    atm = []
    
    for ix in range(Ls[0]):
        for iy in range(Ls[1]):
            for iz in range(Ls[2]):
                shift = [ix * prim_a[0, 0], iy * prim_a[1, 1], iz * prim_a[2, 2]]
                for atom in prim_atm:
                    atm.append([atom[0], (atom[1][0] + shift[0], atom[1][1] + shift[1], atom[1][2] + shift[2])])
    
    Cell.atom = atm
    Cell.basis = basis
    Cell.pseudo = pseudo
    Cell.ke_cutoff = ke_cutoff
    Cell.max_memory = max_memory
    Cell.precision = precision
    Cell.use_particle_mesh_ewald = use_particle_mesh_ewald
    Cell.verbose = 4
    Cell.unit = 'angstorm'
    
    Cell.build(mesh=mesh)
    
    return Cell

####################### Select IP #######################

def _get_possible_IP(pbc_isdf_info:PBC_ISDF_Info, Ls, coords):
    cell = pbc_isdf_info.cell
    ncell = np.prod(Ls)
    
    mesh = cell.mesh
    mesh_prim = np.array(mesh) // np.array(Ls)
    ngrid_prim = np.prod(mesh_prim)
    
    natm = cell.natm
    natm_in_partition = natm // ncell
        
    partition = pbc_isdf_info.partition
    partition_reshaped = partition.reshape(mesh)
    
    possible_primID_selected = np.zeros((ngrid_prim), dtype=np.int32)
    
    tmp = []
    
    for atmid in range(8): ### ????
        # print("atmid = ", atmid)
        where = np.where(partition == atmid)
        # print(where)
        for grid_id in where[0]:
            pnt_id = (grid_id // (mesh[1] * mesh[2]), (grid_id // mesh[2]) % mesh[1], grid_id % mesh[2])
            box_id = (pnt_id[0] // mesh_prim[0], pnt_id[1] // mesh_prim[1], pnt_id[2] // mesh_prim[2])
            pnt_prim_id = (pnt_id[0] % mesh_prim[0], pnt_id[1] % mesh_prim[1], pnt_id[2] % mesh_prim[2])
            pnt_prim_ravel_id = pnt_prim_id[0] * mesh_prim[1] * mesh_prim[2] + pnt_prim_id[1] * mesh_prim[2] + pnt_prim_id[2]
            if box_id[0] == 0 and box_id[1] == 0 and box_id[2] == 0:
                possible_primID_selected[pnt_prim_ravel_id] = 1
            tmp.append((grid_id, box_id, pnt_prim_ravel_id))
    
    # res = []
    
    possible_grid_ID = []
    
    for data in tmp:
        grid_id, box_id, pnt_prim_ravel_id = data
        
        if box_id[0] == 0 and box_id[1] == 0 and box_id[2] == 0:
            # res.append(coords[grid_id])
            possible_grid_ID.append(grid_id)
        else:
            if possible_primID_selected[pnt_prim_ravel_id] == 0: # not selected in the first box
                # res.append(coords[grid_id])
                possible_grid_ID.append(grid_id)
    
    possible_grid_ID.sort()
    # print("possible_grid_ID = ", possible_grid_ID)
    
    return possible_grid_ID, np.array(coords[possible_grid_ID])

  
####################### build aux basis #######################

## Incore 

def _RowCol_FFT_bench(input, Ls, inv=False, TransBra = True, TransKet = True):
    """
    A is a 3D array, (nbra, nket, ngrid_prim)
    """
    
    A = input
    ncell = np.prod(Ls)
    
    assert A.shape[1] % ncell == 0
    assert A.shape[0] % ncell == 0
    
    print("A.shape = ", A.shape)
    print("Ls = ", Ls)
    
    NPOINT_KET = A.shape[1] // ncell
    
    if TransKet:
        A = A.reshape(A.shape[0], -1, NPOINT_KET) # nbra, nBox, NPOINT
        A = A.transpose(0, 2, 1)                  # nbra, NPOINT, nBox
        shape_tmp = A.shape
        A = A.reshape(A.shape[0] * NPOINT_KET, *Ls)
        # perform 3d fft 
        if inv:
            A = np.fft.ifftn(A, axes=(1, 2, 3))
        else:
            A = np.fft.fftn(A, axes=(1, 2, 3))
        A = A.reshape(shape_tmp)
        A = A.transpose(0, 2, 1)
        A = A.reshape(A.shape[0], -1)
        print("finish transform ket")
    # transform bra
    NPOINT_BRA = A.shape[0] // ncell
    if TransBra:
        A = A.reshape(-1, NPOINT_BRA, A.shape[1])
        A = A.transpose(1, 2, 0)
        shape_tmp = A.shape
        A = A.reshape(-1, *Ls)
        if inv:
            A = np.fft.fftn(A, axes=(1, 2, 3))
        else:
            A = np.fft.ifftn(A, axes=(1, 2, 3))
        A = A.reshape(shape_tmp)
        A = A.transpose(2, 0, 1)
        A = A.reshape(-1, A.shape[2])
        print("finish transform bra")
    # print(A[:NPOINT, :NPOINT])
    return A

def _RowCol_FFT_ColFull_bench(input, Ls, mesh):
    """
    A is a 3D array, (nbra, nket, ngrid_prim)
    """
    A = input
    ncell = np.prod(Ls)
    nGrids = np.prod(mesh)
    assert A.shape[1] == nGrids
    assert A.shape[0] % ncell == 0
    A = A.reshape(A.shape[0], *mesh)
    # perform 3d fft 
    A = np.fft.fftn(A, axes=(1, 2, 3))
    A = A.reshape(A.shape[0], -1)
    print("finish transform ket")
    # transform bra
    NPOINT_BRA = A.shape[0] // ncell
    A = A.reshape(-1, NPOINT_BRA, A.shape[1])
    A = A.transpose(1, 2, 0)
    shape_tmp = A.shape
    A = A.reshape(-1, *Ls)
    A = np.fft.ifftn(A, axes=(1, 2, 3))
    A = A.reshape(shape_tmp)
    A = A.transpose(2, 0, 1)
    A = A.reshape(-1, A.shape[2])
    print("finish transform bra")
    return A

def _construct_aux_basis_benchmark(mydf:ISDF.PBC_ISDF_Info):

    aoRg = mydf.aoRg
    coords = mydf.coords
    weight = np.sqrt(mydf.cell.vol / mydf.ngrids)
    aoR_unordered = mydf._numint.eval_ao(mydf.cell, coords)[0].T * weight
    Ls = mydf.Ls
    mesh = mydf.mesh
    meshPrim = np.array(mesh) // np.array(Ls)
    
    ### test the blockdiag matrixstructure ### 
    
    ncell     = np.prod(mydf.Ls)
    mesh      = mydf.mesh
    mesh_prim = np.array(mesh) // np.array(mydf.Ls)
    nGridPrim = mydf.nGridPrim
    nIP_Prim  = mydf.nIP_Prim
    nGrids    = mydf.ngrids
    
    A = np.asarray(lib.ddot(aoRg.T, aoRg), order='C')
    lib.square_inPlace(A)
    
    mydf.aux_basis_bench = np.asarray(lib.ddot(aoRg.T, aoR_unordered), order='C')
    lib.square_inPlace(mydf.aux_basis_bench)
    
    print("mydf.aux_basis_bench = ", mydf.aux_basis_bench.shape)
    
    ### check symmetry ### 
    
    print("B.shape = ", mydf.aux_basis_bench.shape)
    print("nIP_prim = ", nIP_Prim)
    print("nGridPrim = ", nGridPrim)
        
    A = _RowCol_FFT_bench(A, Ls)
    mydf.aux_basis_bench = _RowCol_FFT_ColFull_bench(mydf.aux_basis_bench, Ls, mesh)
    mydf.aux_basis_bench = mydf.aux_basis_bench.reshape(-1, meshPrim[0], Ls[0], meshPrim[1],Ls[1], meshPrim[2], Ls[2])
    mydf.aux_basis_bench = mydf.aux_basis_bench.transpose(0, 2, 4, 6, 1, 3, 5)
    mydf.aux_basis_bench = mydf.aux_basis_bench.reshape(-1, np.prod(mesh))
    
    for i in range(ncell):
        
        b_begin = i * nIP_Prim
        b_end   = (i + 1) * nIP_Prim
        
        k_begin = i * nIP_Prim
        k_end   = (i + 1) * nIP_Prim
        
        matrix_before = A[b_begin:b_end, :k_begin]
        matrix_after  = A[b_begin:b_end, k_end:]
        
        assert np.allclose(matrix_before, 0.0)
        assert np.allclose(matrix_after, 0.0)   
        
        k_begin = i * nGridPrim
        k_end   = (i + 1) * nGridPrim
        
        matrix_before = mydf.aux_basis_bench[b_begin:b_end, :k_begin]
        matrix_after  = mydf.aux_basis_bench[b_begin:b_end, k_end:]
        
        assert np.allclose(matrix_before, 0.0)
        assert np.allclose(matrix_after, 0.0)
    
    A = np.asarray(lib.ddot(aoRg.T, aoRg), order='C')
    lib.square_inPlace(A)
    
    mydf.aux_basis_bench = np.asarray(lib.ddot(aoRg.T, aoR_unordered), order='C')
    lib.square_inPlace(mydf.aux_basis_bench)
    
    mydf.aux_basis_bench = np.linalg.solve(A, mydf.aux_basis_bench)
    
    # perform FFT 
    
    mydf.aux_basis_bench_Grid = mydf.aux_basis_bench.copy()
    
    mydf.aux_basis_bench = _RowCol_FFT_ColFull_bench(mydf.aux_basis_bench, Ls, mesh)
    
    mydf.aux_basis_bench = mydf.aux_basis_bench.reshape(-1, meshPrim[0], Ls[0], meshPrim[1],Ls[1], meshPrim[2], Ls[2])
    mydf.aux_basis_bench = mydf.aux_basis_bench.transpose(0, 2, 4, 6, 1, 3, 5)
    mydf.aux_basis_bench = mydf.aux_basis_bench.reshape(-1, np.prod(mesh))
    
    aux_basis_bench_res = np.zeros((nIP_Prim, nGrids), dtype=np.complex128)

    for icell in range(ncell):
        b_begin = icell * nIP_Prim
        b_end   = (icell + 1) * nIP_Prim
        k_begin = icell * nGridPrim
        k_end   = (icell + 1) * nGridPrim
        
        matrix_before = mydf.aux_basis_bench[b_begin:b_end, :k_begin]
        matrix_after  = mydf.aux_basis_bench[b_begin:b_end, k_end:]
        if np.allclose(matrix_before, 0.0) == False:
            print("Warning Cell %d, matrix_before is not zero" % icell)
        # print("matrix_after = ", matrix_after)
        # assert np.allclose(matrix_after, 0.0, atol=1e-7)
        if np.allclose(matrix_after, 0.0) == False:
            print("Warning Cell %d, matrix_after is not zero" % icell)
        
        aux_basis_bench_res[:, k_begin:k_end] = mydf.aux_basis_bench[b_begin:b_end, k_begin:k_end]

    fac = np.sqrt(np.prod(Ls) / np.prod(mesh)) # normalization factor 

    mydf.aux_basis_bench = aux_basis_bench_res * fac

def _construct_aux_basis_kSym(mydf:ISDF.PBC_ISDF_Info):

    #### get the buffer ####
    
    nGrids   = mydf.ngrids
    nGridPrim = mydf.nGridPrim
    nIP_Prim = mydf.nIP_Prim
    
    Mesh = mydf.mesh
    Mesh = np.array(Mesh, dtype=np.int32)
    Ls   = mydf.Ls
    Ls   = np.array(Ls, dtype=np.int32)
    MeshPrim = np.array(Mesh) // np.array(Ls)
    ncell_complex = Ls[0] * Ls[1] * (Ls[2]//2+1)

    print("nGrids        = ", nGrids)
    print("nGridPrim     = ", nGridPrim)
    print("nIP_Prim      = ", nIP_Prim)
    print("ncell_complex = ", ncell_complex)
    print("Mesh          = ", Mesh)
    print("Ls            = ", Ls)
    print("MeshPrim      = ", MeshPrim)

    naux = mydf.naux
    
    mydf._allocate_jk_buffer() #
    
    buffer1 = np.ndarray((nIP_Prim, ncell_complex*nIP_Prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=0)
    buffer1_real = np.ndarray((nIP_Prim, naux), dtype=np.double, buffer=mydf.jk_buffer, offset=0)
    offset  = nIP_Prim * ncell_complex*nIP_Prim * buffer1.itemsize
    buffer2 = np.ndarray((nIP_Prim, ncell_complex*nGridPrim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    offset += nIP_Prim * ncell_complex*nGridPrim * buffer2.itemsize
    buffer3 = np.ndarray((nIP_Prim, ncell_complex*nGridPrim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    buffer3_real = np.ndarray((nIP_Prim, nGrids), dtype=np.double, buffer=mydf.jk_buffer, offset=offset)
    offset += nIP_Prim * ncell_complex*nGridPrim * buffer3.itemsize
    nthread = lib.num_threads()
    buffer_final_fft = np.ndarray((nthread, nGridPrim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    nao = mydf.nao
    aoR = mydf.aoR
    
    #### do the work ####

    aoRg = mydf.aoRg[:, :nIP_Prim] # only the first box is used
    
    print("aoRg.shape         = ", aoRg.shape)
    print("mydf.aoRg          = ", mydf.aoRg.shape)
    print("buffer1_real.shape = ", buffer1_real.shape)

    A = np.asarray(lib.ddot(aoRg.T, mydf.aoRg, c=buffer1_real), order='C')
    lib.square_inPlace(A)
    
    # mydf.aux_basis = buffer3
    B              = np.asarray(lib.ddot(aoRg.T, mydf.aoR, c=buffer3_real), order='C')
    lib.square_inPlace(B)
    
    ##### FFT #####
    
    fn = getattr(libpbc, "_FFT_Matrix_Col_InPlace", None)
    assert fn is not None
    
    print("A.shape = ", A.shape)
    print("B.shape = ", B.shape)
    print("buffer2.shape = ", buffer2.shape)
    print("mesh = ", Mesh)
    
    fn(
        A.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nIP_Prim),
        ctypes.c_int(nIP_Prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buffer2.ctypes.data_as(ctypes.c_void_p)
    )
    
    fn(
        B.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nIP_Prim),
        ctypes.c_int(nGridPrim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buffer2.ctypes.data_as(ctypes.c_void_p)
    )
    
    print("before solve linear equation")
        
    ##### solve the linear equation #####
    
    A_complex = buffer1
    B_complex = buffer3
    
    mydf.aux_basis = np.zeros((nIP_Prim, nGrids), dtype=np.complex128)
    
    fn_cholesky = getattr(libpbc, "Complex_Cholesky", None)
    assert fn_cholesky is not None
    fn_solve = getattr(libpbc, "Solve_LLTEqualB_Complex_Parallel", None)
    assert fn_solve is not None
    
    nthread = lib.num_threads()
    bunchsize = nGridPrim // nthread
    
    ### after solve linear equation, we have to perform another FFT ### 
    
    freq1 = np.array(range(MeshPrim[0]), dtype=np.float64)
    freq2 = np.array(range(MeshPrim[1]), dtype=np.float64)
    freq3 = np.array(range(MeshPrim[2]), dtype=np.float64)
    freq_q = np.array(np.meshgrid(freq1, freq2, freq3, indexing='ij'))
    
    freq1 = np.array(range(Ls[0]), dtype=np.float64)
    freq2 = np.array(range(Ls[1]), dtype=np.float64)
    freq3 = np.array(range(Ls[2]//2+1), dtype=np.float64)
    freq_Q = np.array(np.meshgrid(freq1, freq2, freq3, indexing='ij'))
    
    FREQ = np.einsum("ijkl,ipqs->ijklpqs", freq_Q, freq_q)
    FREQ[0] /= (Ls[0] * MeshPrim[0])
    FREQ[1] /= (Ls[1] * MeshPrim[1])
    FREQ[2] /= (Ls[2] * MeshPrim[2])
    FREQ = np.einsum("ijklpqs->jklpqs", FREQ)
    FREQ  = FREQ.reshape(-1, np.prod(MeshPrim)).copy()
    FREQ  = np.exp(-2.0j * np.pi * FREQ)  # this is the only correct way to construct the factor
    
    fn_final_fft = getattr(libpbc, "_FinalFFT", None)
    assert fn_final_fft is not None
    fn_permutation_conj = getattr(libpbc, "_PermutationConj", None)
    assert fn_permutation_conj is not None
    
    def _permutation(nx, ny, nz, shift_x, shift_y, shift_z):
        
        res = np.zeros((nx*ny*nz), dtype=numpy.int32)
        
        loc_now = 0
        for ix in range(nx):
            for iy in range(ny):
                for iz in range(nz):
                    ix2 = (nx - ix - shift_x) % nx
                    iy2 = (ny - iy - shift_y) % ny
                    iz2 = (nz - iz - shift_z) % nz
                    
                    loc = ix2 * ny * nz + iy2 * nz + iz2
                    # res[loc_now] = loc
                    res[loc] = loc_now
                    loc_now += 1
        return res
    
    permutation = np.zeros((8, nGridPrim), dtype=np.int32)
    print("permutation.shape = ", permutation.shape)
    permutation[0] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 0, 0, 0)
    permutation[1] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 0, 0, 1)
    permutation[2] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 0, 1, 0)
    permutation[3] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 0, 1, 1)
    permutation[4] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 1, 0, 0)
    permutation[5] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 1, 0, 1)
    permutation[6] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 1, 1, 0)
    permutation[7] = _permutation(MeshPrim[0], MeshPrim[1], MeshPrim[2], 1, 1, 1)
    
    fac = np.sqrt(np.prod(Ls) / np.prod(Mesh)) # normalization factor
    
    i=0
    for ix in range(Ls[0]):
        for iy in range(Ls[1]):
            for iz in range(Ls[2]//2+1):
                
                A_tmp = A_complex[:, i*nIP_Prim:(i+1)*nIP_Prim].copy()
                B_tmp = B_complex[:, i*nGridPrim:(i+1)*nGridPrim].copy()
                
                fn_cholesky(
                    A_tmp.ctypes.data_as(ctypes.c_void_p),
                    ctypes.c_int(nIP_Prim)
                )
        
                fn_solve(
                    ctypes.c_int(nIP_Prim),
                    A_tmp.ctypes.data_as(ctypes.c_void_p),
                    B_tmp.ctypes.data_as(ctypes.c_void_p),
                    ctypes.c_int(B_tmp.shape[1]),
                    ctypes.c_int(bunchsize)
                )
                
                # print("B_tmp = ", B_tmp[:5,:5])
                
                # B_tmp1 = B_tmp.copy()
                # B_tmp1 = B_tmp1 * FREQ[i]
                # B_tmp1 = B_tmp1.reshape(-1, *MeshPrim)
                # B_tmp1 = np.fft.fftn(B_tmp1, axes=(1, 2, 3)) # shit
                # B_tmp1 = B_tmp1.reshape(-1, np.prod(MeshPrim))
                
                # print("B_tmp = ", B_tmp[:5,:5])
                
                fn_final_fft(
                    B_tmp.ctypes.data_as(ctypes.c_void_p),
                    FREQ[i].ctypes.data_as(ctypes.c_void_p),
                    ctypes.c_int(nIP_Prim),
                    ctypes.c_int(nGridPrim),
                    MeshPrim.ctypes.data_as(ctypes.c_void_p),
                    buffer_final_fft.ctypes.data_as(ctypes.c_void_p)
                )
                
                # print("B_tmp1 = ", B_tmp1[:5,:5])
                # print("B_tmp  = ", B_tmp[:5,:5])
                
                # assert np.allclose(B_tmp1, B_tmp)
                
                #### perform the last FFT ####
                
                iloc = ix * Ls[1] * Ls[2] + iy * Ls[2] + iz
                mydf.aux_basis[:, iloc*nGridPrim:(iloc+1)*nGridPrim] = B_tmp
                
                # perform the complex conjugate transpose
                
                ix2 = (Ls[0] - ix) % Ls[0]
                iy2 = (Ls[1] - iy) % Ls[1]
                iz2 = (Ls[2] - iz) % Ls[2]
                
                i+=1
                
                if ix2==ix and iy2==iy and iz2==iz:
                    print("skip the complex conjugate transpose for (ix,iy,iz) = ", ix, iy, iz)
                    continue
                
                perm_id = 0
                if ix != 0:
                    perm_id += 4
                if iy != 0:
                    perm_id += 2
                if iz != 0:
                    perm_id += 1
                
                fn_permutation_conj(
                    B_tmp.ctypes.data_as(ctypes.c_void_p),
                    ctypes.c_int(nIP_Prim),
                    ctypes.c_int(nGridPrim),
                    permutation[perm_id].ctypes.data_as(ctypes.c_void_p),
                    buffer_final_fft.ctypes.data_as(ctypes.c_void_p)
                )
                
                iloc2 = ix2 * Ls[1] * Ls[2] + iy2 * Ls[2] + iz2
                
                mydf.aux_basis[:, iloc2*nGridPrim:(iloc2+1)*nGridPrim] = B_tmp
    
    mydf.aux_basis = mydf.aux_basis * fac
                     
## Outcore

####################### construct W #######################
    
def _construct_W_benchmark_grid(cell, aux_basis:np.ndarray):
    
    def constrcuct_V_CCode(aux_basis:np.ndarray, mesh, coul_G):
        
        coulG_real         = coul_G.reshape(*mesh)[:, :, :mesh[2]//2+1].reshape(-1)
        nThread            = lib.num_threads()
        nAux               = aux_basis.shape[0]
        bunchsize          = nAux // (2*nThread)
        bufsize_per_thread = bunchsize * coulG_real.shape[0] * 2
        bufsize_per_thread = (bufsize_per_thread + 15) // 16 * 16
        ngrids             = aux_basis.shape[1]
        mesh_int32         = np.array(mesh, dtype=np.int32)

        V                  = np.zeros((nAux, ngrids), dtype=np.double)
        buffer             = np.zeros((nThread, bufsize_per_thread), dtype=np.double)

        fn = getattr(libpbc, "_construct_V", None)
        assert(fn is not None)

        print("V.shape = ", V.shape)
        print("aux_basis.shape = ", aux_basis.shape)
        # print("self.jk_buffer.size    = ", self.jk_buffer.size)
        # print("self.jk_buffer.shape   = ", self.jk_buffer.shape)

        fn(mesh_int32.ctypes.data_as(ctypes.c_void_p),
           ctypes.c_int(nAux),
           aux_basis.ctypes.data_as(ctypes.c_void_p),
           coulG_real.ctypes.data_as(ctypes.c_void_p),
           V.ctypes.data_as(ctypes.c_void_p),
           ctypes.c_int(bunchsize),
           buffer.ctypes.data_as(ctypes.c_void_p),
           ctypes.c_int(bufsize_per_thread))

        return V
    
    mesh  = cell.mesh
    coulG = tools.get_coulG(cell, mesh=mesh)
    
    V_R = constrcuct_V_CCode(aux_basis, mesh, coulG)
    
    naux = aux_basis.shape[0]
    
    W = np.zeros((naux,naux))
    lib.ddot(a=aux_basis, b=V_R.T, c=W, beta=1.0)
    
    return W
    
## Incore

def _construct_W_incore(mydf:ISDF.PBC_ISDF_Info):
    
    mydf._allocate_jk_buffer()
    
    mesh  = mydf.mesh
    coulG = tools.get_coulG(mydf.cell, mesh=mesh)
    Ls    = mydf.Ls
    Ls    = np.array(Ls, dtype=np.int32)
    mesh_prim = np.array(mesh) // np.array(Ls)
    coulG = coulG.reshape(mesh_prim[0], Ls[0], mesh_prim[1], Ls[1], mesh_prim[2], Ls[2])
    coulG = coulG.transpose(1, 3, 5, 0, 2, 4).reshape(-1, np.prod(mesh_prim)).copy()
    
    nIP_prim      = mydf.nIP_Prim
    nGrid_prim    = mydf.nGridPrim
    ncell         = np.prod(Ls)
    ncell_complex = Ls[0] * Ls[1] * (Ls[2]//2+1)
    
    #### allocate buffer ####
    
    W = np.zeros((nIP_prim, nIP_prim*ncell), dtype=np.float64)
    
    print("nIP_prim = ", nIP_prim)
    print("nGrid_prim = ", nGrid_prim)
    
    offset  = 0
    A_buf   = np.ndarray((nIP_prim, nGrid_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=0)
    offset += nIP_prim * nGrid_prim * A_buf.itemsize
    print("offset = ", offset//8)
    B_buf   = np.ndarray((nIP_prim, nGrid_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    offset += nIP_prim * nGrid_prim * B_buf.itemsize
    print("offset = ", offset//8)
    W_buf   = np.ndarray((nIP_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    offset += nIP_prim * nIP_prim * W_buf.itemsize
    print("offset = ", offset//8)
    print("ncell_complex = ", ncell_complex)
    print(mydf.jk_buffer.size)
    W_buf2  = np.ndarray((nIP_prim, nIP_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)   
    W_buf3  = np.ndarray((nIP_prim, nIP_prim*ncell), dtype=np.float64, buffer=mydf.jk_buffer, offset=offset)
    offset += nIP_prim * nIP_prim * ncell_complex * W_buf2.itemsize
    W_buf_fft = np.ndarray((nIP_prim, nIP_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)

    fn = getattr(libpbc, "_construct_W_multiG", None)
    assert(fn is not None)

    # for i in range(ncell):
    
    loc = 0
    
    for ix in range(Ls[0]):
        for iy in range(Ls[1]):
            for iz in range(Ls[2]//2+1):
                
                i = ix * Ls[1] * (Ls[2]) + iy * (Ls[2]) + iz
                
                k_begin = i * nGrid_prim
                k_end   = (i + 1) * nGrid_prim
        
                A_buf[:] = mydf.aux_basis[:, k_begin:k_end]

                B_buf[:] = A_buf[:]

                fn(
                    ctypes.c_int(nIP_prim),
                    ctypes.c_int(0),
                    ctypes.c_int(nGrid_prim),
                    B_buf.ctypes.data_as(ctypes.c_void_p),
                    coulG[i].ctypes.data_as(ctypes.c_void_p)
                )
        
                # print("A_buf.shape = ", A_buf.shape)
                # print("B_buf.shape = ", B_buf.shape)
                # print("W_buf.shape = ", W_buf.shape)
            
                # lib.dot(B_buf, A_buf.T.conj(), c=W_buf)
                lib.dot(A_buf, B_buf.T.conj(), c=W_buf)

                k_begin = loc * nIP_prim
                k_end   = (loc + 1) * nIP_prim

                W_buf2[:, k_begin:k_end] = W_buf
            
                loc += 1
    
    fn = getattr(libpbc, "_iFFT_Matrix_Col_InPlace", None)
    assert(fn is not None)
    
    fn(
        W_buf2.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nIP_prim),
        ctypes.c_int(nIP_prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        W_buf_fft.ctypes.data_as(ctypes.c_void_p)
    )
    
    
    W.ravel()[:] = W_buf3.ravel()[:]
    mydf.W = W

    ### used in get J ### 
    
    W0 = np.zeros((nIP_prim, nIP_prim), dtype=np.float64)
    
    for i in range(ncell):
            
            k_begin = i * nIP_prim
            k_end   = (i + 1) * nIP_prim
            
            W0 += W[:, k_begin:k_end]

    mydf.W0 = W0

    return W


## Outcore

####################### get_j #######################
    
def _get_j_with_Wgrid(mydf:ISDF.PBC_ISDF_Info, W_grid=None, dm=None):
    
    if W_grid is None:
        if hasattr(mydf, "W_grid"):
            W_grid = mydf.W_grid
        else:
            mydf.construct_auxbasis_benchmark()
            aux_basis = mydf.aux_basis_bench_Grid
            W_grid = _construct_W_benchmark_grid(mydf.cell, aux_basis)
            mydf.W_grid = W_grid
    print("W_grid.shape = ", W_grid.shape)
    
    W_backup = mydf.W
    mydf.W   = W_grid
    J = ISDF_outcore._get_j_dm_wo_robust_fitting(mydf, dm)
    mydf.W   = W_backup
    return J

def _get_k_with_Wgrid(mydf:ISDF.PBC_ISDF_Info, W_grid=None, dm=None):
    
    # W_backup = mydf.W
    # mydf.W   = W_grid
    # K = ISDF_outcore._get_k_dm_wo_robust_fitting(mydf, dm)
    # mydf.W   = W_backup
    
    if W_grid is None:
        W_grid = mydf.W_grid
    
    t1 = (logger.process_clock(), logger.perf_counter())
    
    aoRg = mydf.aoRg
    
    density_RgRg = lib.dot(dm, aoRg)
    density_RgRg = lib.dot(aoRg.T, density_RgRg)
    lib.cwise_mul(W_grid, density_RgRg, out=density_RgRg)
    K = lib.dot(density_RgRg, aoRg.T)
    K = lib.dot(aoRg, K)
    
    ngrid = np.prod(mydf.cell.mesh)
    vol = mydf.cell.vol
    
    t2 = (logger.process_clock(), logger.perf_counter())
    
    _benchmark_time(t1, t2, "_contract_k_dm")
    
    return K * ngrid / vol
    
def _pack_JK(input_mat:np.ndarray, Ls, nao_prim, output=None):
    
    assert input_mat.dtype == np.float64    
    ncell = np.prod(Ls)
    # print("ncell = ", ncell)
    # print("Ls = ", Ls)  
    # print("nao_prim = ", nao_prim)
    # print("input_mat.shape = ", input_mat.shape)
    assert input_mat.shape[0] == nao_prim
    assert input_mat.shape[1] == nao_prim * ncell
    
    if output is None:
        output = np.zeros((ncell*nao_prim, ncell*nao_prim), dtype=np.float64)  
    else:
        assert output.shape == (ncell*nao_prim, ncell*nao_prim)  
    
    for ix_row in range(Ls[0]):
        for iy_row in range(Ls[1]):
            for iz_row in range(Ls[2]):
                
                loc_row = ix_row * Ls[1] * Ls[2] + iy_row * Ls[2] + iz_row
                
                b_begin = loc_row * nao_prim
                b_end   = (loc_row + 1) * nao_prim
                
                for ix_col in range(Ls[0]):
                    for iy_col in range(Ls[1]):
                        for iz_col in range(Ls[2]):
                            
                            loc_col = ix_col * Ls[1] * Ls[2] + iy_col * Ls[2] + iz_col
                            
                            k_begin = loc_col * nao_prim
                            k_end   = (loc_col + 1) * nao_prim
                            
                            ix = (ix_col - ix_row) % Ls[0]
                            iy = (iy_col - iy_row) % Ls[1]
                            iz = (iz_col - iz_row) % Ls[2]
                            
                            loc_col2 = ix * Ls[1] * Ls[2] + iy * Ls[2] + iz
                            
                            k_begin2 = loc_col2 * nao_prim
                            k_end2   = (loc_col2 + 1) * nao_prim
                            
                            output[b_begin:b_end, k_begin:k_end] = input_mat[:, k_begin2:k_end2]
                            
    return output
    
    
def _get_j_kSym(mydf:ISDF.PBC_ISDF_Info, dm):
    
    ### preprocess
    
    mydf._allocate_jk_buffer(dm.dtype)
    
    t1 = (logger.process_clock(), logger.perf_counter())
    
    if len(dm.shape) == 3:
        assert dm.shape[0] == 1
        dm = dm[0]
    
    nao  = dm.shape[0]
    cell = mydf.cell
    assert cell.nao == nao
    ngrid = np.prod(cell.mesh)
    vol = cell.vol
    
    W         = mydf.W
    aoRg      = mydf.aoRg
    aoRg_Prim = mydf.aoRg_Prim
    # aoR_Prim  = mydf.aoR_Prim
    naux      = aoRg.shape[1]
    
    Ls = mydf.Ls
    mesh = mydf.mesh
    meshPrim = np.array(mesh) // np.array(Ls)
    nGridPrim = mydf.nGridPrim
    ncell = np.prod(Ls)
    ncell_complex = Ls[0] * Ls[1] * (Ls[2]//2+1)
    nIP_prim = mydf.nIP_Prim
    nao_prim = nao // ncell
    
    ### allocate buffer
    
    assert dm.dtype == np.float64
    
    buffer  = mydf.jk_buffer
    
    buffer1 = np.ndarray((nao,nIP_prim),  dtype=dm.dtype, buffer=buffer, offset=0) 
    buffer2 = np.ndarray((nIP_prim),      dtype=dm.dtype, buffer=buffer, offset=(nao * nIP_prim) * dm.dtype.itemsize)
    
    offset  = (nao * nIP_prim + nIP_prim) * dm.dtype.itemsize
    buffer3 = np.ndarray((nao_prim,nao),   dtype=np.float64, buffer=buffer, offset=offset)
    
    offset += (nao_prim * nao) * dm.dtype.itemsize
    bufferW = np.ndarray((nIP_prim,1), dtype=np.float64, buffer=buffer, offset=offset)
    
    offset += (nIP_prim) * dm.dtype.itemsize
    bufferJ_block = np.ndarray((nao_prim, nao_prim), dtype=np.float64, buffer=buffer, offset=offset)
    
    offset += (nao_prim * nao_prim) * dm.dtype.itemsize
    bufferi = np.ndarray((nao_prim,nIP_prim), dtype=np.float64, buffer=buffer, offset=offset)
    
    offset += (nao_prim * nIP_prim) * dm.dtype.itemsize
    bufferj = np.ndarray((nao_prim,nIP_prim), dtype=np.float64, buffer=buffer, offset=offset)
    
    offset += (nao_prim * nIP_prim) * dm.dtype.itemsize
    buffer4 = np.ndarray((nao_prim,nIP_prim), dtype=np.float64, buffer=buffer, offset=offset)
    
    lib.ddot(dm, aoRg_Prim, c=buffer1)
    tmp1 = buffer1
    density_Rg = np.asarray(lib.multiply_sum_isdf(aoRg_Prim, tmp1, out=buffer2), order='C')
    
    ### check the symmetry of density_Rg
    
    # buffer1 = np.ndarray((nao,naux),  dtype=dm.dtype, buffer=buffer, offset=0)
    # buffer2 = np.ndarray((naux),      dtype=dm.dtype, buffer=buffer, offset=(nao * naux) * dm.dtype.itemsize)
    # offset = (nao * naux + naux) * dm.dtype.itemsize
    # buffer3 = np.ndarray((nao_prim,nao),   dtype=np.complex128, buffer=buffer, offset=offset)
    
    # lib.ddot(dm, aoRg, c=buffer1) 
    # tmp1       = buffer1
    # density_Rg = np.asarray(lib.multiply_sum_isdf(aoRg, tmp1, out=buffer2), order='C') 
    
    # for i in range(ncell):
    #     k_begin = i * nIP_prim
    #     k_end   = (i + 1) * nIP_prim
    #     assert np.allclose(density_Rg[k_begin:k_end], density_Rg[:nIP_prim].conj()) # pass 
    
    ### get J
    
    # density_Rg *= np.prod(Ls)
    # W_0 = W[:, :nIP_prim].real # must be real
    # W_0 = W[:, :nIP_prim]
    
    W_0 = mydf.W0
    
    # W_imag = W[:, :nIP_prim].imag
    # assert np.allclose(W_imag, 0.0)
    
    lib.ddot(W_0, density_Rg.reshape(-1,1), c=bufferW)
    bufferW = bufferW.reshape(-1)
    buffer_J = buffer3
    
    for ix_q in range(Ls[0]):
        for iy_q in range(Ls[1]):
            for iz_q in range(Ls[2]):
                
                bufferJ_block.ravel()[:] = 0.0 # set to zero
    
                ### loop over the blocks
                
                for ix in range(Ls[0]):
                    for iy in range(Ls[1]):
                        for iz in range(Ls[2]):
                            
                            ipx = (Ls[0] - ix) % Ls[0]
                            ipy = (Ls[1] - iy) % Ls[1]
                            ipz = (Ls[2] - iz) % Ls[2]
                            
                            loc_p = ipx * Ls[1] * Ls[2] + ipy * Ls[2] + ipz
                            
                            begin = loc_p * nao_prim
                            end   = (loc_p + 1) * nao_prim
                            
                            buffer_i = aoRg_Prim[begin:end, :]
                            
                            iqx = (ix_q - ix + Ls[0]) % Ls[0]
                            iqy = (iy_q - iy + Ls[1]) % Ls[1]
                            iqz = (iz_q - iz + Ls[2]) % Ls[2]
                            
                            loc_q = iqx * Ls[1] * Ls[2] + iqy * Ls[2] + iqz
                            
                            begin = loc_q * nao_prim
                            end   = (loc_q + 1) * nao_prim
                            
                            buffer_j = aoRg_Prim[begin:end, :]
                            tmp = np.asarray(lib.d_ij_j_ij(buffer_j, bufferW, out=buffer4), order='C')
                            lib.ddot_withbuffer(buffer_i, tmp.T, c=bufferJ_block, beta=1, buf=mydf.ddot_buf)
                
                ### set ### 
                
                loc_q = ix_q * Ls[1] * Ls[2] + iy_q * Ls[2] + iz_q
                
                begin = loc_q * nao_prim
                end   = (loc_q + 1) * nao_prim
                
                buffer_J[:, begin:end] = bufferJ_block
    
    buffer_J = buffer_J * (ngrid / vol)
    
    J = _pack_JK(buffer_J, Ls, nao_prim, output=None)
    
    t2 = (logger.process_clock(), logger.perf_counter())
    
    _benchmark_time(t1, t2, "_contract_j_dm")
    
    return J
    
def _get_DM_RgRg_benchmark(mydf:ISDF.PBC_ISDF_Info, dm):
    
    aoRg = mydf.aoRg
    
    tmp1 = lib.ddot(dm, aoRg, c=None)
    
    return lib.ddot(aoRg.T, tmp1, c=None)
    
def _get_DM_RgRg_real(mydf:ISDF.PBC_ISDF_Info, dm_real, dm_complex, dm_RgRg_real, dm_RgRg_complex, offset):
    
    nao   = mydf.nao
    Ls    = np.array(mydf.Ls, dtype=np.int32)
    ncell = np.prod(Ls)
    ncell_complex = Ls[0] * Ls[1] * (Ls[2]//2+1)
    nao_prim  = nao // ncell
    nIP_prim  = mydf.nIP_Prim
    mesh      = np.array(mydf.mesh, dtype=np.int32)  
    meshPrim  = np.array(mesh) // np.array(Ls)
    nGridPrim = mydf.nGridPrim
    
    fn1 = getattr(libpbc, "_FFT_Matrix_Col_InPlace", None)
    assert fn is not None
    
    buf_fft = np.ndarray((nao_prim, nao_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    # buf_fft = np.zeros((nao_prim, nao_prim*ncell_complex), dtype=np.complex128)
    
    fn1(
        dm_real.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nao_prim),
        ctypes.c_int(nao_prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buf_fft.ctypes.data_as(ctypes.c_void_p)
    )
    
    buf_A = np.ndarray((nao_prim, nao_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    offset2 = offset + (nao_prim * nao_prim) * buf_A.itemsize
    buf_B = np.ndarray((nao_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset2)
    
    offset3 = offset2 + (nao_prim * nIP_prim) * buf_B.itemsize
    buf_C = np.ndarray((nao_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset3)
    
    offset4 = offset3 + (nao_prim * nIP_prim) * buf_C.itemsize
    buf_D = np.ndarray((nIP_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset4)
    
    aoRg_FFT = mydf.aoRg_FFT
    
    for i in range(ncell_complex):
        
        k_begin = i * nao_prim
        k_end   = (i + 1) * nao_prim
        
        buf_A[:] = dm_complex[:, k_begin:k_end]
        buf_B[:] = aoRg_FFT[:, i*nIP_prim:(i+1)*nIP_prim]
        
        lib.dot(buf_A, buf_B, c=buf_C)
        lib.dot(buf_B.T.conj(), buf_C, c=buf_D)
        
        k_begin = i * nIP_prim
        k_end   = (i + 1) * nIP_prim
        
        dm_RgRg_complex[:, k_begin:k_end] = buf_D
    
    buf_fft = np.ndarray((nIP_prim, nIP_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    fn2 = getattr(libpbc, "_iFFT_Matrix_Col_InPlace", None)
    assert fn is not None
    
    fn2(
        dm_RgRg_complex.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nIP_prim),
        ctypes.c_int(nIP_prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buf_fft.ctypes.data_as(ctypes.c_void_p)
    )
    
    # print("dm_RgRg_complex = ", dm_RgRg_complex[:5,:5])
    
    return dm_RgRg_real
    
def _get_k_kSym(mydf:ISDF.PBC_ISDF_Info, dm):
    
    #### checkt the symmetry of dm ####
    
    # Ls = np.array(mydf.Ls, dtype=np.int32)
    # dm_fft = _RowCol_FFT_bench(dm, Ls)
    # nao_prim = dm_fft.shape[0] // np.prod(Ls)
    # for i in range(np.prod(Ls)):
    #     b_begin = i * nao_prim
    #     b_end   = (i + 1) * nao_prim
    #     k_begin = i * nao_prim
    #     k_end   = (i + 1) * nao_prim
    #     matrix_before = dm_fft[b_begin:b_end, :k_begin]
    #     matrix_after  = dm_fft[b_begin:b_end, k_end:]
    #     # assert np.allclose(matrix_before, 0.0)
    #     if np.allclose(matrix_before, 0.0, atol=1.0e-8)==False:
    #         print("cell ", i, " matrix_before is not zero with max = ", np.max(np.abs(matrix_before)))
    #     # assert np.allclose(matrix_after, 0.0, atol=1.0e-8)
    #     if np.allclose(matrix_after, 0.0, atol=1.0e-8)==False:
    #         print("cell ", i, " matrix_after is not zero with max = ", np.max(np.abs(matrix_after)))
        
    
    #### preprocess ####  
    
    mydf._allocate_jk_buffer(dm.dtype)
    t1 = (logger.process_clock(), logger.perf_counter())
    
    if len(dm.shape) == 3:
        assert dm.shape[0] == 1
        dm = dm[0]
    
    nao  = dm.shape[0]
    cell = mydf.cell    
    assert cell.nao == nao
    ngrid = np.prod(cell.mesh)
    vol = cell.vol
    
    W         = mydf.W
    aoRg      = mydf.aoRg
    aoRg_Prim = mydf.aoRg_Prim
    naux      = aoRg.shape[1]
    
    Ls = np.array(mydf.Ls, dtype=np.int32)
    mesh = mydf.mesh
    meshPrim = np.array(mesh) // np.array(Ls)
    nGridPrim = mydf.nGridPrim
    ncell = np.prod(Ls)
    ncell_complex = Ls[0] * Ls[1] * (Ls[2]//2+1)
    nIP_prim = mydf.nIP_Prim
    nao_prim = nao // ncell
    
    #### allocate buffer ####
     
    
    offset = 0
    DM_RgRg_complex = np.ndarray((nIP_prim,nIP_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    DM_RgRg_real = np.ndarray((nIP_prim,nIP_prim*ncell), dtype=np.float64, buffer=mydf.jk_buffer, offset=offset)
    
    offset += (nIP_prim * nIP_prim * ncell_complex) * DM_RgRg_complex.itemsize
    DM_complex = np.ndarray((nao_prim,nao_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    DM_real = np.ndarray((nao_prim,nao), dtype=np.float64, buffer=mydf.jk_buffer, offset=offset)
    DM_real.ravel()[:] = dm[:nao_prim, :].ravel()[:]
    offset += (nao_prim * nao_prim * ncell_complex) * DM_complex.itemsize
    
    #### get D ####
    
    #_get_DM_RgRg_real(mydf, DM_real, DM_complex, DM_RgRg_real, DM_RgRg_complex, offset)
    
    fn1 = getattr(libpbc, "_FFT_Matrix_Col_InPlace", None)
    assert fn1 is not None
    
    buf_fft = np.ndarray((nao_prim, nao_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
        
    fn1(
        DM_real.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nao_prim),
        ctypes.c_int(nao_prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buf_fft.ctypes.data_as(ctypes.c_void_p)
    )
    
    buf_A = np.ndarray((nao_prim, nao_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    offset2 = offset + (nao_prim * nao_prim) * buf_A.itemsize
    buf_B = np.ndarray((nao_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset2)
    
    offset3 = offset2 + (nao_prim * nIP_prim) * buf_B.itemsize
    buf_C = np.ndarray((nao_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset3)
    
    offset4 = offset3 + (nao_prim * nIP_prim) * buf_C.itemsize
    buf_D = np.ndarray((nIP_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset4)
    
    aoRg_FFT = mydf.aoRg_FFT
    
    for i in range(ncell_complex):
        
        k_begin = i * nao_prim
        k_end   = (i + 1) * nao_prim
        
        buf_A[:] = DM_complex[:, k_begin:k_end]
        buf_B[:] = aoRg_FFT[:, i*nIP_prim:(i+1)*nIP_prim]
        
        lib.dot(buf_A, buf_B, c=buf_C)
        lib.dot(buf_B.T.conj(), buf_C, c=buf_D)
        
        k_begin = i * nIP_prim
        k_end   = (i + 1) * nIP_prim
        
        DM_RgRg_complex[:, k_begin:k_end] = buf_D
    
    buf_fft = np.ndarray((nIP_prim, nIP_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    fn2 = getattr(libpbc, "_iFFT_Matrix_Col_InPlace", None)
    assert fn2 is not None
    
    fn2(
        DM_RgRg_complex.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nIP_prim),
        ctypes.c_int(nIP_prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buf_fft.ctypes.data_as(ctypes.c_void_p)
    )
    
    # inplace multiplication
    
    lib.cwise_mul(mydf.W, DM_RgRg_real, out=DM_RgRg_real)
    
    offset = nIP_prim * nIP_prim * ncell_complex * DM_RgRg_complex.itemsize
    
    buf_fft = np.ndarray((nIP_prim, nIP_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    fn1(
        DM_RgRg_real.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nIP_prim),
        ctypes.c_int(nIP_prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buf_fft.ctypes.data_as(ctypes.c_void_p)
    )
    
    K_complex_buf = np.ndarray((nao_prim, nao_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    K_real_buf    = np.ndarray((nao_prim, nao_prim*ncell), dtype=np.float64, buffer=mydf.jk_buffer, offset=offset)
    offset += (nao_prim * nao_prim * ncell_complex) * K_complex_buf.itemsize
    offset_now = offset    
    
    buf_A = np.ndarray((nIP_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset_now)
    offset_now += (nIP_prim * nIP_prim) * buf_A.itemsize
    buf_B = np.ndarray((nao_prim, nIP_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset_now)
    offset_now += (nao_prim * nIP_prim) * buf_B.itemsize
    buf_C = np.ndarray((nIP_prim, nao_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset_now)
    offset_now += (nIP_prim * nao_prim) * buf_C.itemsize
    buf_D = np.ndarray((nao_prim, nao_prim), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset_now)
    
    for i in range(ncell_complex):
        
        k_begin = i * nIP_prim
        k_end   = (i + 1) * nIP_prim
        
        buf_A.ravel()[:] = DM_RgRg_complex[:, k_begin:k_end].ravel()[:]
        buf_B.ravel()[:] = aoRg_FFT[:, i*nIP_prim:(i+1)*nIP_prim].ravel()[:]
        
        lib.dot(buf_A, buf_B.T.conj(), c=buf_C)
        lib.dot(buf_B, buf_C, c=buf_D)
        
        k_begin = i * nao_prim
        k_end   = (i + 1) * nao_prim
        
        K_complex_buf[:, k_begin:k_end] = buf_D
    
    buf_fft = np.ndarray((nao_prim, nao_prim*ncell_complex), dtype=np.complex128, buffer=mydf.jk_buffer, offset=offset)
    
    fn2(
        K_complex_buf.ctypes.data_as(ctypes.c_void_p),
        ctypes.c_int(nao_prim),
        ctypes.c_int(nao_prim),
        Ls.ctypes.data_as(ctypes.c_void_p),
        buf_fft.ctypes.data_as(ctypes.c_void_p)
    )
    
    K_real_buf *= (ngrid / vol)
    
    K = _pack_JK(K_real_buf, Ls, nao_prim, output=None)
    
    t2 = (logger.process_clock(), logger.perf_counter())
    
    _benchmark_time(t1, t2, "_contract_k_dm")
    
    return K
    
    # return DM_RgRg_real # temporary return for debug

def _symmetrize_dm(dm, Ls):
        
    ncell = np.prod(Ls)
    nao = dm.shape[0]
    nao_prim = nao // ncell
    dm_symm = np.zeros((nao,nao), dtype=dm.dtype)
        
    for i in range(Ls[0]):
        for j in range(Ls[1]):
            for k in range(Ls[2]):
                
                dm_symmized_buf = np.zeros((nao_prim,nao_prim), dtype=dm.dtype)
                
                for i_row in range(Ls[0]):
                    for j_row in range(Ls[1]):
                        for k_row in range(Ls[2]):
                            
                            loc_row = i_row * Ls[1] * Ls[2] + j_row * Ls[2] + k_row
                            loc_col = ((i + i_row) % Ls[0]) * Ls[1] * Ls[2] + ((j + j_row) % Ls[1]) * Ls[2] + (k + k_row) % Ls[2]
                            
                            b_begin = loc_row * nao_prim
                            b_end   = (loc_row + 1) * nao_prim
                            
                            k_begin = loc_col * nao_prim
                            k_end   = (loc_col + 1) * nao_prim
                            
                            dm_symmized_buf += dm[b_begin:b_end, k_begin:k_end]
        
                dm_symmized_buf /= ncell
                
                for i_row in range(Ls[0]):
                    for j_row in range(Ls[1]):
                        for k_row in range(Ls[2]):
                            
                            loc_row = i_row * Ls[1] * Ls[2] + j_row * Ls[2] + k_row
                            loc_col = ((i + i_row) % Ls[0]) * Ls[1] * Ls[2] + ((j + j_row) % Ls[1]) * Ls[2] + (k + k_row) % Ls[2]
                            
                            b_begin = loc_row * nao_prim
                            b_end   = (loc_row + 1) * nao_prim
                            
                            k_begin = loc_col * nao_prim
                            k_end   = (loc_col + 1) * nao_prim
                            
                            dm_symm[b_begin:b_end, k_begin:k_end] = dm_symmized_buf        
        
    return dm_symm

def get_jk_dm_kSym(mydf, dm, hermi=1, kpt=np.zeros(3),
                   kpts_band=None, with_j=True, with_k=True, omega=None, **kwargs):
    '''JK for given k-point'''

    if len(dm.shape) == 3:
        assert dm.shape[0] == 1
        dm = dm[0]

    assert with_j is True and with_k is True

    #### explore the linearity of J K with respect to dm ####

    #### perform the calculation ####

    if mydf.jk_buffer is None:  # allocate the buffer for get jk
        # mydf._allocate_jk_buffer(mydf, datatype=dm.dtype)
        mydf._allocate_jk_buffer(datatype=dm.dtype)

    if "exxdiv" in kwargs:
        exxdiv = kwargs["exxdiv"]
    else:
        exxdiv = None

    vj = vk = None

    if kpts_band is not None and abs(kpt-kpts_band).sum() > 1e-9:
        raise NotImplementedError("ISDF does not support kpts_band != kpt")

    log = logger.Logger(mydf.stdout, mydf.verbose)
    t1 = (logger.process_clock(), logger.perf_counter())

    j_real = gamma_point(kpt)
    k_real = gamma_point(kpt) and not np.iscomplexobj(dm)

    assert j_real
    assert k_real

    mem_now = lib.current_memory()[0]
    max_memory = max(2000, (mydf.max_memory - mem_now))

    log.debug1('max_memory = %d MB (%d in use)', max_memory, mem_now)

    dm = _symmetrize_dm(dm, mydf.Ls)

    if mydf.outcore and mydf.with_robust_fitting == True:
        raise NotImplementedError("outcore robust fitting has bugs and is extremely slow.")
        # vj, vk = _get_jk_dm_outcore(mydf, dm)
    else:
        if mydf.with_robust_fitting == True:
            # vj = _get_j_dm_wo_robust_fitting(mydf, dm)
            # vk = _get_k_dm_wo_robust_fitting(mydf, dm)
            raise NotImplementedError
        else:
            # print(dm[:10, :10])
            vj1 = _get_j_kSym(mydf, dm)
            vk2 = _get_k_kSym(mydf, dm)
            # vj = _get_j_with_Wgrid(mydf, dm=dm)
            # vk = _get_k_with_Wgrid(mydf, dm=dm)
            # vj = isdf_jk._contract_j_dm(mydf, dm, mydf.with_robust_fitting)
            # vk = isdf_jk._contract_k_dm(mydf, dm, mydf.with_robust_fitting)
            # print(vk[:4,:4])
            # print(vk2[:4,:4])
            # print(vk[:4,:4]/vk2[:4,:4])
            # assert np.allclose(vj, vj1)
            # assert np.allclose(vk, vk2)
            
            # for i in range(vk.shape[0]):
            #     for j in range(vk.shape[1]):
            #         if abs(vk[i,j] - vk2[i, j])/abs(vk[i,j]) > 1.0e-4:
            #             print("vk [", i, ",", j, "] = ", vk[i,j])
            #             print("vk2[", i, ",", j, "] = ", vk2[i,j])
            
            # print("max_diff vj = ", np.max(np.abs(vj - vj1)))
            # print("max_diff vk = ", np.max(np.abs(vk - vk2)))
            
            # nao_prim = mydf.nao // np.prod(mydf.Ls)
            
            # dm11 = dm[:nao_prim, :nao_prim]
            # dm22 = dm[nao_prim:, nao_prim:]
            # print("max_diff dm11 - dm22 = ", np.max(np.abs(dm11 - dm22)))
            
            vj = vj1
            vk = vk2

    t1 = log.timer('sr jk', *t1)

    return vj, vk

class PBC_ISDF_Info_kSym(ISDF_outcore.PBC_ISDF_Info_outcore):
    def __init__(self, mol:Cell, max_buf_memory:int, Ls=[1,1,1], outcore=True, with_robust_fitting=True, aoR=None):
        
        super().__init__(mol=mol, max_buf_memory=max_buf_memory, outcore=outcore, with_robust_fitting=with_robust_fitting, aoR=aoR)
        
        assert with_robust_fitting == False
        assert self.mesh[0] % Ls[0] == 0
        assert self.mesh[1] % Ls[1] == 0
        assert self.mesh[2] % Ls[2] == 0
        
        self.Ls = Ls
        
        if self.coords is None:
            from pyscf.pbc.dft.multigrid.multigrid_pair import MultiGridFFTDF2
            df_tmp = MultiGridFFTDF2(self.cell)
            self.coords = np.asarray(df_tmp.grids.coords).reshape(-1,3).copy()

        print("self.cell.lattice_vectors = ", self.cell.lattice_vectors())
        self.ordered_grid_coords, self.ordered_grid_coords_dict = _extract_grid_primitive_cell(self.cell.lattice_vectors(), self.mesh, self.Ls, self.coords)

        # self.aoR_Prim = self._numint.eval_ao(self.cell, self.ordered_grid_coords_dict[(0,0,0)])[0].T

        if outcore is False:
            weight   = np.sqrt(self.cell.vol / self.ngrids)
            self.aoR = self._numint.eval_ao(self.cell, self.ordered_grid_coords)[0].T * weight # the T is important
    
    ################ test function ################ 
    
    def construct_auxbasis_benchmark(self):
        _construct_aux_basis_benchmark(self)
    
    ################ allocate buffer ################ 
    
    def _allocate_jk_buffer(self, dtype=np.float64):
        
        if self.jk_buffer is not None:
            return
            
        num_threads = lib.num_threads()
        
        nIP_Prim = self.nIP_Prim
        nGridPrim = self.nGridPrim
        ncell_complex = self.Ls[0] * self.Ls[1] * (self.Ls[2]//2+1)
        nao_prim  = self.nao // np.prod(self.Ls)
        
        if self.outcore is False:
            
            ### in build aux basis ###
            
            size_buf1 = nIP_Prim * ncell_complex*nIP_Prim * 2
            size_buf1+= nIP_Prim * ncell_complex*nGridPrim * 2 * 2
            size_buf1+= num_threads * nGridPrim * 2
            
            ### in construct W ###
            
            # print("nIP_Prim = ", nIP_Prim)
            # print("ncell_complex = ", ncell_complex)    
            
            size_buf2  = nIP_Prim * nIP_Prim * 2
            size_buf2 += nIP_Prim * nGridPrim * 2 * 2
            size_buf2 += nIP_Prim * nIP_Prim *  ncell_complex * 2 * 2
            
            # print("size_buf2 = ", size_buf2)
            
            ### in get_j ###
            
            naux       = self.naux
            nao        = self.nao
            size_buf3  = nao * naux + naux + naux + nao * nao
            
            size_buf4  = nao * nIP_Prim
            size_buf4 += nIP_Prim
            size_buf4 += nao_prim * nao
            size_buf4 += nIP_Prim
            size_buf4 += nao_prim * nao_prim
            size_buf4 += nao_prim * nIP_Prim * 3
            
            ### in get_k ### 
            
            size_buf5  = nIP_Prim * nIP_Prim * ncell_complex * 2
            size_buf5 += nao_prim * nao_prim * 2
            size_buf5 += nIP_Prim * nIP_Prim * ncell_complex * 2
            
            size_buf6  = nIP_Prim * nIP_Prim * ncell_complex * 2
            size_buf6 += nIP_Prim * nIP_Prim * ncell_complex * 2
            size_buf6 += nao_prim * nao_prim * ncell_complex * 2
            size_buf6 += nIP_Prim * nIP_Prim  * 2
            size_buf6 += nao_prim * nIP_Prim  * 2 * 2
            size_buf6 += nao_prim * nao_prim  * 2
            
            ### ddot_buf ###
            
            size_ddot_buf = max(naux*naux+2,ngrids)*num_threads
            
            # print("size_buf1 = ", size_buf1)
            # print("size_buf2 = ", size_buf2)
            # print("size_buf3 = ", size_buf3)
            # print("size_buf4 = ", size_buf4)
            # print("size_buf5 = ", size_buf5)
            
            size_buf = max(size_buf1,size_buf2,size_buf3,size_buf4,size_buf5,size_buf6)
            
            # print("size_buf = ", size_buf)
            
            if hasattr(self, "IO_buf"):
                if self.IO_buf.size < (size_buf+size_ddot_buf):
                    self.IO_buf = np.zeros((size_buf+size_ddot_buf), dtype=np.float64)
                self.jk_buffer = np.ndarray((size_buf), dtype=np.float64, buffer=self.IO_buf, offset=0)
                # offset         = max(buffersize_k, buffersize_j) * self.jk_buffer.dtype.itemsize
                # self.ddot_buf  = np.ndarray((nThreadsOMP, max((naux*naux)+2, ngrids)),
                #                             dtype=np.float64, buffer=self.IO_buf, offset=offset)
                self.ddot_buf = np.ndarray((size_ddot_buf), dtype=np.float64, buffer=self.IO_buf, offset=size_buf)

            else:

                self.jk_buffer = np.ndarray((size_buf), dtype=np.float64)
                # self.ddot_buf = np.zeros((nThreadsOMP, max((naux*naux)+2, ngrids)), dtype=np.float64) 
                self.ddot_buf = np.zeros((size_ddot_buf), dtype=np.float64)
            
        else:
            raise NotImplementedError
    
    ################ select IP ################
    
    def select_IP(self, c:int, m:int):
        first_natm = self.cell.natm // np.prod(self.Ls)
        
        print("first_natm = ", first_natm)
        
        IP_GlobalID = ISDF._select_IP_direct(self, c, m, first_natm, True) # we do not have to perform selection IP over the whole supercell ! 
        
        print("len of IP_GlobalID = ", len(IP_GlobalID))
        
        # get primID
        
        mesh = self.cell.mesh
        mesh_prim = np.array(mesh) // np.array(self.Ls)
        ngrid_prim = np.prod(mesh_prim)
                
        possible_grid_ID = []
    
        for grid_id in IP_GlobalID:
            pnt_id = (grid_id // (mesh[1] * mesh[2]), (grid_id // mesh[2]) % mesh[1], grid_id % mesh[2])
            box_id = (pnt_id[0] // mesh_prim[0], pnt_id[1] // mesh_prim[1], pnt_id[2] // mesh_prim[2])
            pnt_prim_id = (pnt_id[0] % mesh_prim[0], pnt_id[1] % mesh_prim[1], pnt_id[2] % mesh_prim[2])
            pnt_prim_ravel_id = pnt_prim_id[0] * mesh_prim[1] * mesh_prim[2] + pnt_prim_id[1] * mesh_prim[2] + pnt_prim_id[2]
            # print("grid_id = %d, pnt_id = %s, box_id = %s, pnt_prim_id = %s" % (grid_id, pnt_id, box_id, pnt_prim_id))
            possible_grid_ID.append(pnt_prim_ravel_id)

        possible_grid_ID = list(set(possible_grid_ID))
        possible_grid_ID.sort()
        
        print("nIP = ", len(possible_grid_ID))
        print("possible_grid_ID = ", possible_grid_ID)
        
        ordered_IP_coords = []
        
        # print("self.ordered_grid_coords_dict = ", self.ordered_grid_coords_dict.keys())
        
        for ix in range(self.Ls[0]):
            for iy in range(self.Ls[1]):
                for iz in range(self.Ls[2]):
                    ordered_IP_coords.append(self.ordered_grid_coords_dict[(ix,iy,iz)][possible_grid_ID]) # enforce translation symmetry
        
        self.ordered_IP_coords = np.array(ordered_IP_coords).reshape(-1,3).copy()
        
        grid_primitive = self.ordered_grid_coords_dict[(0,0,0)]
        # self.IP_coords = grid_primitive[possible_grid_ID]
        weight         = np.sqrt(self.cell.vol / self.ngrids)
        self.aoRg      = self._numint.eval_ao(self.cell, self.ordered_IP_coords)[0].T * weight
        self.aoRg_Prim = self.aoRg[:, :len(possible_grid_ID)].copy()
        self.nGridPrim = grid_primitive.shape[0]
        self.nIP_Prim  = len(possible_grid_ID)
        
        nao_prim = self.nao // np.prod(self.Ls)
        Ls       = np.array(self.Ls, dtype=np.int32)    
        ncell_complex = self.Ls[0] * self.Ls[1] * (self.Ls[2]//2+1)
        
        # self.aoRg_FFT  = self.aoRg[:nao_prim,:].copy()
        self.aoRg_FFT  = np.zeros((nao_prim, ncell_complex*self.nIP_Prim), dtype=np.complex128)
        self.aoRg_FFT_real = np.ndarray((nao_prim, np.prod(Ls)*self.nIP_Prim), dtype=np.double, buffer=self.aoRg_FFT, offset=0)
        self.aoRg_FFT_real.ravel()[:] = self.aoRg[:nao_prim,:].ravel()
        
        nthread        = lib.num_threads()
        buffer         = np.ndarray((nao_prim, ncell_complex*self.nIP_Prim), dtype=np.complex128, buffer=self.jk_buffer, offset=0)
        
        fn = getattr(libpbc, "_FFT_Matrix_Col_InPlace", None)
        assert fn is not None
        
        print("self.aoRg_FFT.shape = ", self.aoRg_FFT.shape)
        
        fn(
            self.aoRg_FFT_real.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_int(nao_prim),
            ctypes.c_int(self.nIP_Prim),
            Ls.ctypes.data_as(ctypes.c_void_p),
            buffer.ctypes.data_as(ctypes.c_void_p)
        ) # no normalization factor ! 
                
        return np.array(possible_grid_ID, dtype=np.int32)
        
    ################ construct W ################

    ################ driver for build ################
    
    def build_IP_auxbasis(self, IO_File:str = None, c:int = 5, m:int = 5):
        
        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        self.IP_ID = self.select_IP(c, m)  # prim_gridID
        self.IP_ID = np.asarray(self.IP_ID, dtype=np.int32)
        print("IP_ID = ", self.IP_ID)
        print("len(IP_ID) = ", len(self.IP_ID))
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        _benchmark_time(t1, t2, "select IP")

        if IO_File is None:
            # generate a random file name start with tmp_
            import random
            import string
            IO_File = "tmp_" + ''.join(random.choices(string.ascii_lowercase + string.digits, k=8)) + ".hdf5"

        print("IO_File = ", IO_File)

        # construct aoR

        if self.coords is None:
            from pyscf.pbc.dft.multigrid.multigrid_pair import MultiGridFFTDF2
            df_tmp = MultiGridFFTDF2(self.cell)
            self.coords = np.asarray(df_tmp.grids.coords).reshape(-1,3).copy()

        # t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        # coords_IP = self.coords[self.IP_ID]
        # weight    = np.sqrt(self.cell.vol / self.ngrids)
        # self.aoRg = self._numint.eval_ao(self.cell, coords_IP)[0].T * weight  # the T is important
        # t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        # _benchmark_time(t1, t2, "construct aoR") # built in select_IP

        self.naux = self.aoRg.shape[1]
        print("naux = ", self.naux)
        self.c    = c
        
        # print("naux = ", self.naux)
        # self.chunk_size, self.nRow_IO_V, self.blksize_aux, self.bunchsize_readV, self.grid_bunchsize, self.blksize_W, self.use_large_chunk_W  = _determine_bunchsize(
        #     self.nao, self.naux, self.mesh, self.IO_buf.size, self.saveAoR)

        # construct aux basis

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())
        if self.outcore:
            # print("construct aux basis in outcore mode")
            # _construct_aux_basis_IO(self, IO_File, self.IO_buf)
            raise NotImplementedError   
        else:
            print("construct aux basis in incore mode")
            _construct_aux_basis_kSym(self)
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        _benchmark_time(t1, t2, "construct aux basis")

        self.IO_FILE = IO_File
        
    def build_auxiliary_Coulomb(self):

        t1 = (lib.logger.process_clock(), lib.logger.perf_counter())

        if self.outcore:
            raise NotImplementedError
        else:
            
            print("construct W in incore mode")
            _construct_W_incore(self)
        
        t2 = (lib.logger.process_clock(), lib.logger.perf_counter())
        _benchmark_time(t1, t2, "construct W")
    
    ################ get jk ################

    get_jk = get_jk_dm_kSym

C = 5
M = 5

if __name__ == "__main__":
    
    boxlen = 3.5668
    prim_a = np.array([[boxlen,0.0,0.0],[0.0,boxlen,0.0],[0.0,0.0,boxlen]])
    
    KE_CUTOFF = 16
    
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
    
    prim_cell = build_supercell(atm, prim_a, Ls = [1,1,1], ke_cutoff=KE_CUTOFF)
    prim_mesh = prim_cell.mesh
    print("prim_mesh = ", prim_mesh)
    
    # Ls = [2, 2, 2]
    # Ls = [2, 1, 3]
    Ls = [1, 1, 2]
    mesh = [Ls[0] * prim_mesh[0], Ls[1] * prim_mesh[1], Ls[2] * prim_mesh[2]]
    
    cell = build_supercell(atm, prim_a, Ls = Ls, ke_cutoff=KE_CUTOFF, mesh=mesh)
    
    for i in range(cell.natm):
        print('%s %s  charge %f  xyz %s' % (cell.atom_symbol(i),
                                        cell.atom_pure_symbol(i),
                                        cell.atom_charge(i),
                                        cell.atom_coord(i)))

    print("Atoms' charges in a vector\n%s" % cell.atom_charges())
    print("Atoms' coordinates in an array\n%s" % cell.atom_coords())
    
    from pyscf.pbc.dft.multigrid.multigrid_pair import MultiGridFFTDF2

    df_tmp = MultiGridFFTDF2(cell)

    grids  = df_tmp.grids
    coords = np.asarray(grids.coords).reshape(-1,3)
    nx = grids.mesh[0]

    mesh   = grids.mesh
    ngrids = np.prod(mesh)
    assert ngrids == coords.shape[0]

    aoR   = df_tmp._numint.eval_ao(cell, coords)[0].T  # the T is important
    aoR  *= np.sqrt(cell.vol / ngrids)

    print("aoR.shape = ", aoR.shape)

    # pbc_isdf_info = PBC_ISDF_Info(cell, aoR)
    # _, Possible_IP_coords = _get_possible_IP(pbc_isdf_info, Ls, coords)
    # print("Possible_IP_coords = ", Possible_IP_coords)
    
    ############ construct ISDF object ############
    
    pbc_isdf_info_ksym = PBC_ISDF_Info_kSym(cell, 8 * 1000 * 1000, Ls=Ls, outcore=False, with_robust_fitting=False, aoR=None)
    
    ############ test select IP ############
    
    possible_IP = pbc_isdf_info_ksym.select_IP(C, M)
    print("possible_IP = ", possible_IP)
    
    pbc_isdf_info_ksym.build_IP_auxbasis(c=C, m=M)
    pbc_isdf_info_ksym.construct_auxbasis_benchmark()
    
    basis1 = pbc_isdf_info_ksym.aux_basis
    basis2 = pbc_isdf_info_ksym.aux_basis_bench
    
    print("basis1.shape = ", basis1.shape)
    print("basis2.shape = ", basis2.shape)
    
    # print(basis1[:10,:10])
    # print(basis2[:10,:10])
    # print(basis1[:10,:10]/basis2[:10,:10])
    
    # print(basis1[-10:, -10:])
    # print(basis2[-10:, -10:])
    # print(basis1[-10:, -10:]/basis2[-10:, -10:])
    
    assert np.allclose(basis1, basis2) # we get the same result, correct ! 
    
    basis3 = pbc_isdf_info_ksym.aux_basis_bench_Grid
    print("basis3.shape = ", basis3.shape)
    print("basis3.dtype = ", basis3.dtype)
    
    W_grid = _construct_W_benchmark_grid(cell, basis3)
    
    # print("W_grid = ", W_grid[:4,:4])
    
    print("W_grid.shape = ", W_grid.shape)
    # W_grid = W_grid.reshape(-1, mesh[0], mesh[1], mesh[2])
    # print("W_grid.shape = ", W_grid.shape)
    # W_grid = np.fft.fftn(W_grid, axes=(1, 2, 3))
    # W_grid = W_grid.reshape(-1, np.prod(mesh))
    # W_grid = W_grid.reshape(mesh[0], mesh[1], mesh[2], -1)
    # W_grid = W_grid.transpose(3, 0, 1, 2)
    # W_grid = np.fft.ifftn(W_grid, axes=(1, 2, 3))
    # W_grid = W_grid.transpose(1, 2, 3, 0)
    # W_grid = W_grid.reshape(-1, np.prod(mesh))
    
    # W_grid = _RowCol_FFT_bench(W_grid, Ls)
    
    nIP_prim = pbc_isdf_info_ksym.nIP_Prim

    # print(W_grid[:nIP_prim, nIP_prim:2*nIP_prim])
    # print(W_grid[nIP_prim:2*nIP_prim,:nIP_prim])
    
    
    W_fft  = _RowCol_FFT_bench(W_grid, Ls)
    
    # print(W_fft[:nIP_prim, :nIP_prim])
    
    ## check W_fft's diagonal structure ##
    
    ncell = np.prod(Ls)
    
    W_fft_packed = np.zeros((nIP_prim, nIP_prim*ncell), dtype=np.complex128)

    
    for icell in range(ncell):
        b_begin = icell * nIP_prim
        b_end   = (icell + 1) * nIP_prim
        k_begin = icell * nIP_prim
        k_end   = (icell + 1) * nIP_prim
        
        matrix_before = W_fft[b_begin:b_end, :k_begin]
        matrix_after  = W_fft[b_begin:b_end, k_end:]
        assert np.allclose(matrix_before, 0.0)
        assert np.allclose(matrix_after, 0.0)
        
        mat = W_fft[b_begin:b_end, k_begin:k_end]
        assert np.allclose(mat, mat.T.conj())
        
        W_fft_packed[:, k_begin:k_end] = W_fft[b_begin:b_end, k_begin:k_end]
    
    pbc_isdf_info_ksym.build_auxiliary_Coulomb()
    
    W = pbc_isdf_info_ksym.W
    W_bench = W_grid[:nIP_prim, :]
    
    # print(W_fft_packed.shape)
    # print(W.shape)
    
    W1 = W[:4, :4]
    W2 = W_bench[:4, :4]
    
    print(W1)
    print(W2)
    print(W1/W2)
    
    assert np.allclose(W, W_bench)
    
    # exit(1)
    
    from pyscf.pbc import dft as pbcdft
    mf=pbcdft.RKS(cell)
    mf.xc = "PBE,PBE"
    mf.init_guess='atom'  # atom guess is fast
    # mf.with_df = multigrid.MultiGridFFTDF2(cell)
    # mf.with_df.ngrids = 4  # number of sets of grid points ? ? ? 

    dm1 = mf.get_init_guess(cell, 'atom')
    
    J_bench = _get_j_with_Wgrid(pbc_isdf_info_ksym, W_grid, dm1)
    
    J2      = _get_j_kSym(pbc_isdf_info_ksym, dm1)
    
    print(J_bench[:4, :4])
    print(J2[:4, :4])
    
    assert np.allclose(J_bench, J2)  # we get the correct answer!
    
    ## check the symmetry of dm1 ## 
    
    dm_packed = _RowCol_FFT_bench(dm1, Ls)
    
    ncell = np.prod(Ls)
    nao   = cell.nao
    nao_prim = nao // ncell
    
    for icell in range(ncell):
        b_begin = icell * nao_prim
        b_end   = (icell + 1) * nao_prim
        k_begin = icell * nao_prim
        k_end   = (icell + 1) * nao_prim
        
        matrix_before = dm_packed[b_begin:b_end, :k_begin]
        matrix_after  = dm_packed[b_begin:b_end, k_end:]
        assert np.allclose(matrix_before, 0.0)
        assert np.allclose(matrix_after, 0.0)
        
        mat = dm_packed[b_begin:b_end, k_begin:k_end]
        assert np.allclose(mat, mat.T.conj())
    
    # DM_bencharmk = _get_DM_RgRg_benchmark(pbc_isdf_info_ksym, dm1)
    # DM_test2 = _get_k_kSym(pbc_isdf_info_ksym, dm1)
    # print(DM_bencharmk[:4, :4])
    # print(DM_test2[:4, :4])
    
    # assert np.allclose(DM_bencharmk[:nIP_prim,:], DM_test2)
    
    K_bench = _get_k_with_Wgrid(pbc_isdf_info_ksym, W_grid, dm1)
    K2      = _get_k_kSym(pbc_isdf_info_ksym, dm1)
    
    # print(K_bench[:4, :4])
    # print(K2[:4, :4])
    # print(K_bench[:4, :4]/K2[:4, :4])
    
    assert np.allclose(K_bench, K2)  # we get the correct answer!
    
    ### do the SCF ### 
    
    boxlen = 3.5668
    prim_a = np.array([[boxlen,0.0,0.0],[0.0,boxlen,0.0],[0.0,0.0,boxlen]])
    
    KE_CUTOFF = 70
    
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
    
    prim_cell = build_supercell(atm, prim_a, Ls = [1,1,1], ke_cutoff=KE_CUTOFF)
    prim_mesh = prim_cell.mesh
    print("prim_mesh = ", prim_mesh)
    
    C = 15
    
    # Ls = [2, 2, 2]
    Ls = [1, 2, 2]
    Ls = np.array(Ls, dtype=np.int32)
    mesh = [Ls[0] * prim_mesh[0], Ls[1] * prim_mesh[1], Ls[2] * prim_mesh[2]]
    mesh = np.array(mesh, dtype=np.int32)
    
    cell = build_supercell(atm, prim_a, Ls = Ls, ke_cutoff=KE_CUTOFF, mesh=mesh)
    
    pbc_isdf_info = PBC_ISDF_Info_kSym(cell, 80 * 1000 * 1000, Ls=Ls, outcore=False, with_robust_fitting=False, aoR=None)
    pbc_isdf_info.build_IP_auxbasis(c=C, m=M)
    pbc_isdf_info.build_auxiliary_Coulomb()
    
    from pyscf.pbc import scf
    
    mf = scf.RHF(cell)
    pbc_isdf_info.direct_scf = mf.direct_scf
    mf.with_df = pbc_isdf_info
    mf.max_cycle = 16
    mf.conv_tol = 1e-7

    print("mf.direct_scf = ", mf.direct_scf)

    mf.kernel()
        
    exit(1)
        
    mf = scf.RHF(cell)
    pbc_isdf_info.direct_scf = mf.direct_scf
    # mf.with_df = pbc_isdf_info
    mf.max_cycle = 64
    mf.conv_tol = 1e-7

    print("mf.direct_scf = ", mf.direct_scf)

    mf.kernel()
    