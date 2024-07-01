

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

import pyscf.pbc.df.isdf.thc_cc_helper._einsum_holder as einsum_holder

einsum = einsum_holder.thc_einsum_sybolic

# This is restricted (R)CCSD
# Ref: Hirata et al., J. Chem. Phys. 120, 2581 (2004)

### Eqs. (37)-(39) "kappa"

def cc_Foo(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    foo = einsum_holder._expr_foo()
    if eris is None:
        eris_ovov = einsum_holder._thc_eri_ovov()
    else:
        eris_ovov = eris.ovov
    Fki  = 2*einsum('kcld,ilcd->ki', eris_ovov, t2, cached=True)
    Fki -=   einsum('kdlc,ilcd->ki', eris_ovov, t2, cached=True)
    Fki += 2*einsum('kcld,ic,ld->ki', eris_ovov, t1, t1, cached=True)
    Fki -=   einsum('kdlc,ic,ld->ki', eris_ovov, t1, t1, cached=True)
    Fki += foo
    return Fki

def cc_Fvv(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    fvv = einsum_holder._expr_fvv()
    if eris is None:
        eris_ovov = einsum_holder._thc_eri_ovov()
    else:
        eris_ovov = eris.ovov
    Fac  =-2*einsum('kcld,klad->ac', eris_ovov, t2, cached=True)
    Fac +=   einsum('kdlc,klad->ac', eris_ovov, t2, cached=True)
    Fac -= 2*einsum('kcld,ka,ld->ac', eris_ovov, t1, t1, cached=True)
    Fac +=   einsum('kdlc,ka,ld->ac', eris_ovov, t1, t1, cached=True)
    Fac += fvv
    return Fac

def cc_Fov(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    fov = einsum_holder._expr_fov()
    if eris is None:
        eris_ovov = einsum_holder._thc_eri_ovov()
    else:
        eris_ovov = eris.ovov
    Fkc  = 2*einsum('kcld,ld->kc', eris_ovov, t1, cached=True)
    Fkc -=   einsum('kdlc,ld->kc', eris_ovov, t1, cached=True)
    Fkc += fov
    return Fkc

### Eqs. (40)-(41) "lambda"

def Loo(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    fov = einsum_holder._expr_fov()
    Lki = cc_Foo(t1, t2, eris) + einsum('kc,ic->ki',fov, t1, cached=True)
    if eris is None:
        eris_ovoo = einsum_holder._thc_eri_ovoo()
    else:
        eris_ovoo = eris.ovoo
    Lki += 2*einsum('lcki,lc->ki', eris_ovoo, t1, cached=True)
    Lki -=   einsum('kcli,lc->ki', eris_ovoo, t1, cached=True)
    return Lki

def Lvv(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    fov = einsum_holder._expr_fov()
    if eris is None:
        eris_ovvv = einsum_holder._thc_eri_ovvv()
    else:
        eris_ovvv = eris.ovvv
    Lac = cc_Fvv(t1, t2, eris) - einsum('kc,ka->ac',fov, t1, cached=True)
    Lac += 2*einsum('kdac,kd->ac', eris_ovvv, t1, cached=True)
    Lac -=   einsum('kcad,kd->ac', eris_ovvv, t1, cached=True)
    return Lac

### Eqs. (42)-(45) "chi"

def cc_Woooo(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    if eris is None:
        eris_ovoo = einsum_holder._thc_eri_ovoo()
        eris_ovov = einsum_holder._thc_eri_ovov()
        eris_oooo = einsum_holder._thc_eri_oooo()
    else:
        eris_ovoo = eris.ovoo
        eris_ovov = eris.ovov
        eris_oooo = eris.oooo
    Wklij  = einsum('lcki,jc->klij', eris_ovoo, t1)
    Wklij += einsum('kclj,ic->klij', eris_ovoo, t1)
    Wklij += einsum('kcld,ijcd->klij', eris_ovov, t2)
    Wklij += einsum('kcld,ic,jd->klij', eris_ovov, t1, t1)
    #Wklij += np.asarray(eris.oooo).transpose(0,2,1,3)
    Wklij += eris_oooo.transpose((0,2,1,3))
    return Wklij

def cc_Wvvvv(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    if eris is None:
        eris_ovvv = einsum_holder._thc_eri_ovvv()
        eris_vvvv = einsum_holder._thc_eri_vvvv()
    else:
        eris_ovvv = eris.ovvv
        eris_vvvv = eris.vvvv
    Wabcd  = einsum('kdac,kb->abcd', eris_ovvv,-t1)
    Wabcd -= einsum('kcbd,ka->abcd', eris_ovvv, t1)
    Wabcd += eris_vvvv.transpose((0,2,1,3))
    return Wabcd

def cc_Wvoov(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    if eris is None:
        eris_ovvv = einsum_holder._thc_eri_ovvv()
        eris_ovoo = einsum_holder._thc_eri_ovoo()
        eris_ovvo = einsum_holder._thc_eri_ovvo()
        eris_ovov = einsum_holder._thc_eri_ovov()
    else:
        eris_ovvv = eris.ovvv
        eris_ovoo = eris.ovoo
        eris_ovvo = eris.ovvo
        eris_ovov = eris.ovov
    Wakic  = einsum('kcad,id->akic', eris_ovvv, t1)
    Wakic -= einsum('kcli,la->akic', eris_ovoo, t1)
    Wakic += eris_ovvo.transpose((2,0,3,1))
    Wakic -= 0.5*einsum('ldkc,ilda->akic', eris_ovov, t2)
    Wakic -= 0.5*einsum('lckd,ilad->akic', eris_ovov, t2)
    Wakic -= einsum('ldkc,id,la->akic', eris_ovov, t1, t1)
    Wakic += einsum('ldkc,ilad->akic', eris_ovov, t2)
    return Wakic

def cc_Wvovo(t1:einsum_holder._expr_holder, t2:einsum_holder._expr_holder, eris=None):
    if eris is None:
        eris_ovvv = einsum_holder._thc_eri_ovvv()
        eris_ovoo = einsum_holder._thc_eri_ovoo()
        eris_oovv = einsum_holder._thc_eri_oovv()
        eris_ovov = einsum_holder._thc_eri_ovov()
    else:
        eris_ovvv = eris.ovvv
        eris_ovoo = eris.ovoo
        eris_oovv = eris.oovv
        eris_ovov = eris.ovov
    Wakci  = einsum('kdac,id->akci', eris_ovvv, t1)
    Wakci -= einsum('lcki,la->akci', eris_ovoo, t1)
    Wakci += eris_oovv.transpose((2,0,3,1))
    Wakci -= 0.5*einsum('lckd,ilda->akci', eris_ovov, t2)
    Wakci -= einsum('lckd,id,la->akci', eris_ovov, t1, t1)
    return Wakci