#!/usr/bin/env python
import os, sys, shutil
from forcebalance.molecule import *
from forcebalance.gmxio import *
from forcebalance.openmmio import *
from forcebalance.nifty import _exec, queue_up_src_dest, wq_wait, printcool
import mdtraj as md
import numpy as np
import random as rd
from scipy.cluster.hierarchy import linkage, fcluster
import work_queue
from collections import OrderedDict
from matplotlib import pyplot as plt
import argparse

def makeWorkQueue(wq_port):
    #Set up the work_queue structures
    work_queue.set_debug_flag('all')
    wq = work_queue.WorkQueue(port=wq_port, exclusive=False, shutdown=False)
    wq.specify_keepalive_interval(8640000)
    wq.specify_name('reopt')
    print('Work Queue listening on {0}'.format(wq.port))
    return wq

def getClusterIndices(index_file, res_list):
    #A file with indices for the RMSD can be provided. This function
    #looks through this file and returns the indices for each residue.
    indices = {}
    for line in open(index_file):
        if line.split('\n')[0].strip() in res_list:
            res = line.split('\n')[0].strip()
            indices[res] = []
        if line.strip().split('\n')[0].isdigit():
            indices[res].append(int(line.strip().split('\n')[0]))
    return indices

def p_norm(data, p=2):
    """
    Gets pnorm of array, taken from MSMBuilder 2.7 Legacy.
    https://github.com/msmbuilder/msmbuilder-legacy/blob/master/MSMBuilder/clustering.py
    """

    if p == "max":
        return data.max()
    else:
        p = float(p)
        n = float(data.shape[0])
        return ((data**p).sum()/n)**(1.0/p)

def kcenters(traj, dist, atom_indices):
    """
    This clustering algorithm is modified from MSMBuilder 2.7 Legacy.
    https://github.com/msmbuilder/msmbuilder-legacy/blob/master/MSMBuilder/clustering.py
    """
    k = sys.maxsize
    seed = 0
    distance_list = np.inf * np.ones(len(traj), dtype=np.float32)
    assignments = -1 * np.ones(len(traj), dtype=np.int32)

    generator_indices = []
    for i in range(k):
        new_ind = seed if i == 0 else np.argmax(distance_list)
        if distance_list[new_ind] < dist:
            break
        new_distance_list = md.rmsd(traj, traj, new_ind, atom_indices=atom_indices)
        updated_indices = np.where(new_distance_list < distance_list)[0]
        distance_list[updated_indices] = new_distance_list[updated_indices]
        assignments[updated_indices] = new_ind
        generator_indices.append(new_ind)
    return np.array(generator_indices), assignments, distance_list

def hybrid_kmedoids(traj, dist, atom_indices):
    """
    This clustering algorithm is modified from MSMBuilder 2.7 Legacy.
    https://github.com/msmbuilder/msmbuilder-legacy/blob/master/MSMBuilder/clustering.py
    """
    initial_medoids, initial_assignments, initial_distance = kcenters(traj, dist, atom_indices)
    assignments = initial_assignments
    distance_to_current = initial_distance
    medoids = initial_medoids
    pgens = traj[medoids]
    k = len(initial_medoids)
    norm_exponent = 2.0
    num_iters = 10
    too_close_cutoff = 0.0001

    obj_func = p_norm(distance_to_current, p=norm_exponent)
    max_norm = p_norm(distance_to_current, p='max')

    for iteration in range(num_iters):
        for medoid_i in range(k):
            trial_medoid = rd.choice(np.where(assignments == medoids[medoid_i])[0])
            old_medoid = medoids[medoid_i]
            if old_medoid == trial_medoid: continue
            new_medoids = medoids.copy()
            new_medoids[medoid_i] = trial_medoid 
            pmedoids = traj[new_medoids]

            new_distances = distance_to_current.copy()
            new_assignments = assignments.copy()
            
            distance_to_trial = md.rmsd(traj, traj, trial_medoid, atom_indices=atom_indices)
            if distance_to_trial[old_medoid] < too_close_cutoff: continue

            assigned_to_trial = np.where(distance_to_trial < distance_to_current)[0]
            new_assignments[assigned_to_trial] = trial_medoid
            new_distances[assigned_to_trial] = distance_to_trial[assigned_to_trial]

            ambiguous = np.where((new_assignments == old_medoid) & (distance_to_trial >= distance_to_current))[0]

            for l in ambiguous:
                d = md.rmsd(pmedoids, traj, l, atom_indices=atom_indices)
                argmin = np.argmin(d)
                new_assignments[l] = new_medoids[argmin]
                new_distances[l] = d[argmin]

            new_obj_func = p_norm(new_distances, p=norm_exponent)
            new_max_norm = p_norm(new_distances, p='max')

            if new_obj_func < obj_func and (new_max_norm <= max_norm):
                medoids = new_medoids 
                assignments = new_assignments
                distance_to_current = new_distances
                obj_func = new_obj_func
                max_norm = new_max_norm
    return medoids, assignments, distance_to_current

def isfloat(num):
    try:
        float(num)
        return True
    except ValueError:
        return False

def getChargesMult(target_list, charges_input, mult_input):
    charges = {}
    mult = {}
    for key in target_list:
        if '-' in key: res = key.split('-')[0]
        elif '_' in key: res = key.split('_')[0]
        else: res = key
        if not any(res in i for i in charges):
            if charges_input is not None:
                if res in charges_input:
                    ind = charges_input.index(res)
                    charges[res] = charges_input[ind+1]
            else: charges[res] = 0
        if not any(res in i for i in mult):
            if mult_input is not None:
                if res in mult_input:
                    ind = mult_input.index(res)
                    mult[res] = mult_input[ind+1]
            else: mult[res] = 1
    return charges, mult

class MMTask:
    def __init__(self, md_engine_opts, wq, coord_set, top_set, ffname, remote_md):
        self.md_engine_opts = md_engine_opts
        self.wq = wq
        self.coord_set = coord_set
        self.top_set = top_set
        self.ffname = ffname
        self.remote_md = remote_md

    def cluster(self, fnm):
        ext = fnm.split('.')[1]
        _exec('find . -name {0} | sort | xargs cat > mmopt.{1}'.format(fnm, ext))
        traj = md.load('mmopt.{0}'.format(ext))
        M = Molecule('mmopt.{0}'.format(ext))
        return traj, M

class QMTask:
    def __init__(self, fbdir, qm_method, basis, cbs, grad, nt, mem):
        self.fbdir = fbdir
        self.qm_method = qm_method
        self.basis = basis
        self.cbs = cbs
        self.grad = grad
        self.nt = nt
        self.mem = mem

class GMXTask(MMTask):
    def __init__(self, md_engine_opts, wq, coord_set, top_set, ffname, remote_md):
        #Get the work_queue and coordinates from the main Reopt_MM class
        super().__init__(md_engine_opts, wq, coord_set, top_set, ffname, remote_md)

    def min_grids(self, res, cwd, min_file):
        #Use specific GROMACS routines for grid minimization.
        with open('topol.top', 'w') as f:
            f.write('#include "{0}"\n'.format(self.ffname.split('/')[-1]))
            for line in open("../../{0}".format(self.top_set[res])):
                f.write(line)
        self.md_engine_opts['mol'] = self.coord_set[res]
        self.md_engine_opts['gmx_top'] = 'topol.top'
        if not os.path.exists(self.ffname.split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.ffname), './{0}'.format(self.ffname.split('/')[-1]))
        engine = GMX(**self.md_engine_opts)
        os.chdir('../')
        result_dir = os.getcwd()
        for i in range(len(self.coord_set[res])):
            print("\rRunning Frame {0}/{1}\r".format(i+1, len(self.coord_set[res])))
            idir = os.path.join(res, "{0:04d}".format(i))
            if not os.path.exists(idir): os.makedirs(idir)
            os.chdir(idir)
            if not os.path.exists(min_file):
                self.coord_set[res][i].write("conf.gro")
                if not os.path.exists(self.ffname.split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.ffname), './{0}'.format(self.ffname.split('/')[-1]))
                if not os.path.exists('gmx.top'): os.symlink('../gmx.top', './gmx.top')
                if not os.path.exists('gmx.mdp'): os.symlink('../gmx.mdp', './gmx.mdp')
                if self.remote_md is None:
                    engine.optimize(i, **self.md_engine_opts)
                else:
                    if self.wq is None:
                        raise Exception("Supply a work queue port for the code if you want to use the remote script functionality.")
                    if not os.path.exists("min.mdp"):
                        min_opts = {"integrator" : "l-bfgs", "emtol" : 1e-4, "nstxout" : 0, "nstfout" : 0, "nsteps" : 10000, "nstenergy" : 1}
                        edit_mdp(fin="gmx.mdp", fout="min.mdp", options=min_opts)
                    queue_up_src_dest(self.wq, command="sh {0} &> min.log".format(self.remote_md), tag=idir,
                    input_files = ([(os.path.join(os.getcwd(), i), i) for i in [self.ffname.split('/')[-1], 'conf.gro', 'topol.top', 'min.mdp']] +
                                   [(os.path.join(cwd, self.remote_md), self.remote_md)]),
                    output_files = [(os.path.join(os.getcwd(), 'min.log'), 'min.log'),
                                   (os.path.join(os.getcwd(), min_file), min_file)])

            os.chdir(result_dir)

    def cluster(self, fnm):
        #Cluster structures to provide back to the main code.
        traj, M = super().cluster(fnm)
        return traj, M

    def cluster_min_MM(self, pdbfnm, cwd, cmdn, min_file, res):
        #Use GROMACS utilities to minimize clustered structures.
        if not os.path.exists(min_file):
            coord = Molecule(os.path.join('../../Clusters', pdbfnm))
            coord[0].write("conf.gro")
            if not os.path.exists(self.ffname.split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.ffname), './{0}'.format(self.ffname.split('/')[-1]))
            if not os.path.exists('topol.top'): os.symlink('../../topol.top', './topol.top')
            self.md_engine_opts['mol'] = coord[0]
            self.md_engine_opts['gmx_top'] = 'topol.top'
            if self.remote_md is None:
                engine = GMX(**self.md_engine_opts)
                engine.optimize(**self.md_engine_opts)
            else:
                if self.wq is None:
                    raise RuntimeError("Supply a work queue port for the code if you want to use the remote script functionality.")
                if not os.path.exists("min.mdp"):
                    min_opts = {"integrator" : "l-bfgs", "emtol" : 1e-4, "nstxout" : 0, "nstfout" : 0, "nsteps" : 10000, "nstenergy" : 1}
                    edit_mdp(fin="gmx.mdp", fout="min.mdp", options=min_opts)
                queue_up_src_dest(self.wq, command="sh rungmx.sh &> min.log", tag=cdnm,
                input_files = ([(os.path.join(os.getcwd(), i), i) for i in [self.ffname.split('/')[-1], 'conf.gro', 'topol.top', 'min.mdp']] +
                [(os.path.join(cwd, 'rungmx.sh'), 'rungmx.sh')]),
                output_files = [(os.path.join(os.getcwd(), 'min.log'), 'min.log'),
                (os.path.join(os.getcwd(), min_file), min_file)])

    def cluster_singlepoint_MM(self, min_file, res):
        #Use GROMACS routines to get the energy for a single molecule.
        self.md_engine_opts['mol'] = Molecule(min_file)
        self.md_engine_opts['gmx_top'] = 'topol.top'
        md = GMX(**self.md_engine_opts)
        energy = md.energy_one(shot=0)
        return energy

class OpenMMTask(MMTask):
    def __init__(self, md_engine_opts, wq, coord_set, top_set, ffname, remote_md):
        #Get the work_queue and coordinates from the main Reopt_MM class
        super().__init__(md_engine_opts, wq, coord_set, top_set, ffname, remote_md)

    def min_grids(self, res, cwd, min_file):
        os.chdir('../')
        result_dir = os.getcwd()
        if not os.path.exists(self.ffname.split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.ffname), './{0}'.format(self.ffname.split('/')[-1]))
        if not os.path.exists(self.top_set[res].split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.top_set[res]), self.top_set[res].split('/')[-1])
        self.md_engine_opts['mol'] = self.coord_set[res]
        self.md_engine_opts['pdb'] = self.top_set[res].split('/')[-1]
        self.md_engine_opts['ffxml'] = self.ffname.split('/')[-1]
        self.md_engine_opts['precision'] = 'double'
        self.md_engine_opts['platform'] = 'Reference'
        engine = OpenMM(**self.md_engine_opts)
        for i in range(len(self.coord_set[res])):
            print("\rRunning Frame {0}/{1}\r".format(i+1, len(self.coord_set[res])))
            idir = os.path.join(res, "{0:04d}".format(i))
            if not os.path.exists(idir): os.makedirs(idir)
            os.chdir(idir)
            if not os.path.exists(min_file):
                self.coord_set[res][i].write("conf.pdb")
                if not os.path.exists(self.ffname.split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.ffname), './{0}'.format(self.ffname.split('/')[-1]))
                if not os.path.exists(self.top_set[res].split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.top_set[res]), self.top_set[res].split('/')[-1])
                engine.optimize(i)
                top = engine.pdb.topology
                pos = engine.getContextPosition(removeVirtual=True)
                PDBFile.writeFile(top, pos, open(min_file, 'w'))
            os.chdir(result_dir)

    def cluster(self, fnm):
        #Cluster structures to provide back to the main code.
        traj, M = super().cluster(fnm)
        return traj, M

    def cluster_min_MM(self, pdbfnm, cwd, cdnm, min_file, res):
        #Use OpenMM utilities to minimize clustered structures.
        if not os.path.exists(min_file):
            coord = Molecule(os.path.join('../../Clusters', pdbfnm))
            coord[0].write("conf.pdb")
            if not os.path.exists(self.ffname.split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.ffname), './{0}'.format(self.ffname.split('/')[-1]))
            if not os.path.exists(self.top_set[res].split('/')[-1]): os.symlink('{0}/{1}'.format(cwd,self.top_set[res]), self.top_set[res].split('/')[-1])
            self.md_engine_opts['mol'] = coord
            self.md_engine_opts['pdb'] = self.top_set[res].split('/')[-1]
            self.md_engine_opts['ffxml'] = self.ffname.split('/')[-1]
            self.md_engine_opts['precision'] = 'double'
            self.md_engine_opts['platform'] = 'Reference'
            engine = OpenMM(**self.md_engine_opts)
            engine.optimize(0)
            top = engine.pdb.topology
            pos = engine.getContextPosition(removeVirtual=True)
            PDBFile.writeFile(top, pos, open(min_file, 'w'))

    def cluster_singlepoint_MM(self, min_file, res):
        #Use OpenMM routines to get the energy for a single molecule.
        self.md_engine_opts['mol'] = Molecule(min_file)
        self.md_engine_opts['pdb'] = self.top_set[res].split('/')[-1]
        self.md_engine_opts['ffxml'] = self.ffname.split('/')[-1]
        self.md_engine_opts['precision'] = 'double'
        self.md_engine_opts['platform'] = 'Reference'
        md = OpenMM(**self.md_engine_opts)
        md.update_simulation()
        energy = md.energy_one(0)
        return energy

class Psi4Task(QMTask):
    def __init__(self, fbdir, qm_method, basis, cbs, grad, nt, mem):
        super().__init__(fbdir, qm_method, basis, cbs, grad, nt, mem)
        loc = shutil.which("psi4")
        if loc is None:
            raise Exception("The psi4 executable has not been found. Make sure you have it installed in your path.")

    def writeEnergy(self, mol, fnm, charge, mult):
        if self.cbs is True:
            self.writeCBSenergy(mol, fnm, charge, mult)
        else:
            out = open(fnm, 'w')
            out.write("memory {0} gb\n\n".format(self.mem))
            out.write("molecule {\n")
            out.write("{0} {1}\n".format(charge, mult))
            for e, c in zip(mol.elem, mol.xyzs[0]):
                out.write("  {0}   {1:9.6f}   {2:9.6f}   {3:9.6f}\n".format(e, c[0], c[1], c[2]))
            out.write("units angstrom\n")
            out.write("no_reorient\n")
            out.write("}\n\n")
            out.write("set globals {\n")
            out.write("basis       {0}\n".format(self.basis))
            out.write("guess       sad\n")
            out.write("scf_type    df\n")
            out.write("puream      true\n")
            out.write("print       1\n")
            out.write("}\n\n")
            out.write("energy('{0}')".format(self.qm_method))
            out.close()

    def writeCBSenergy(self, mol, fnm, charge, mult):
        out = open(fnm, 'w')
        out.write("memory {0} gb\n\n".format(self.mem))
        out.write("molecule {\n")
        out.write("{0} {1}\n".format(charge, mult))
        for e, c in zip(mol.elem, mol.xyzs[0]):
            out.write("  {0}   {1:9.6f}   {2:9.6f}   {3:9.6f}\n".format(e, c[0], c[1], c[2]))
        out.write("units angstrom\n")
        out.write("no_reorient\n")
        out.write("}\n\n")
        out.write("set globals {\n")
        out.write("basis       {0}\n".format(self.basis))
        out.write("guess       sad\n")
        out.write("scf_type    df\n")
        out.write("puream      true\n")
        out.write("print       1\n")
        out.write("}\n\n")
        out.write("energy(cbs, corl_wfn='{0}', corl_basis='aug-cc-pv[tq]z', corl_scheme=corl_xtpl_helgaker_2)".format(self.qm_method))
        out.close()

    def custom_energy(self, mol, fnm, charge, mult, custom_eng):
        eng_lines = []
        for line in open(custom_eng):
            eng_lines.append(line)
        out = open(fnm, 'w')
        out.write("memory {0} gb\n\n".format(self.mem))
        out.write("molecule {\n")
        out.write("{0} {1}\n".format(charge, mult))
        for e, c in zip(mol.elem, mol.xyzs[0]):
            out.write("  {0}   {1:9.6f}   {2:9.6f}   {3:9.6f}\n".format(e, c[0], c[1], c[2]))
        out.write("units angstrom\n")
        out.write("no_reorient\n")
        out.write("}\n\n")
        out.write("set globals {\n")
        out.write("basis       {0}\n".format(self.basis))
        out.write("guess       sad\n")
        out.write("scf_type    df\n")
        out.write("puream      true\n")
        out.write("print       1\n")
        out.write("}\n\n")
        for line in eng_lines:
            out.write("{0}".format(line))
        out.close()

    def writeGrad(self, mol, fnm, charge, mult):
        out = open(fnm, 'w')
        out.write("memory {0} gb\n\n".format(self.mem))
        out.write("molecule {\n")
        out.write("{0} {1}\n".format(charge, mult))
        for e, c in zip(mol.elem, mol.xyzs[0]):
            out.write("  {0}   {1:9.6f}   {2:9.6f}   {3:9.6f}\n".format(e, c[0], c[1], c[2]))
        out.write("units angstrom\n")
        out.write("no_reorient\n")
        out.write("}\n\n")
        out.write("set globals {\n")
        out.write("basis       {0}\n".format(self.basis))
        out.write("guess       sad\n")
        out.write("scf_type    df\n")
        out.write("puream      true\n")
        out.write("print       1\n")
        out.write("}\n\n")
        out.write("gradient('{0}')".format(self.qm_method))
        out.close()

    def readEnergy(self, fnm):
        found_coords = False
        energy = None
        coords = []
        elem = []
        for line in open(fnm):
            ls = line.split()
            if "molecule {" in line:
                found_coords = True
            if found_coords is True:
                if len(ls)==4:
                    elem.append(ls[0])
                    coords.append([float(s) for s in ls[1:4]])
            if "}" in line:
                found_coords = False
            #This is the standard default case with this program
            if "energy('mp2')" in line or "energy('MP2')" in line:
                energy = self.readMP2energy(fnm)
                break
            #This should match any CBS energy calls
            elif "CBS" in line:
                energy = self.readCBSenergy(fnm)
                break
            #Hopefully this gets any other energy calls that could be used
            elif "Total Energy" in line or "total energy" in line or "Total energy" in line:
                energy = self.read_std_energy(fnm)
                break
        if len(coords)==0:
            raise Exception("The coordinates in {0}/grad.out have length zero".format(os.getcwd()))
        if energy is None:
            raise Exception("Oops, the energy output file {0}/energy.out either doesn't match any of the prepared parsers or something went wrong with the calculation. Either contact John Stoppelman to fix it or write your own parser definition yourself.".format(os.getcwd()))
        return energy, elem, coords

    def readMP2energy(self, fnm):
        found_energy = False
        energy = None
        for line in open(fnm):
            ls = line.split()
            if "DF-MP2 Energies" in line:
                found_energy = True
            if found_energy is True and "Total Energy" in line:
                energy = float(ls[-2])
                found_energy = False
        return energy

    def read_std_energy(self, fnm):
        found_energy = False
        energy = None
        for line in reversed(open(fnm).readlines()):
            ls = line.split()
            if "Total Energy" in line or "total energy" in line or "Total energy" in line:
                energy = float(ls[-1])
                break
        return energy

    def readCBSenergy(self, fnm):
        energy = None
        for line in reversed(open(fnm).readlines()):
            ls = line.split()
            if len(ls)==3:
                if ls[0]=="total" and ls[1]=="CBS":
                    energy = float(ls[2])
                    break
        return energy

    def readGrad(self, fnm):
        found_grad = False
        found_coords = False
        elem = []
        coords = []
        grad = []
        for line in open(fnm):
            ls = line.split()
            if "molecule {" in line:
                found_coords = True
            if found_coords is True:
                if len(ls)==4:
                    elem.append(ls[0])
                    coords.append(ls[1:4])
            if "}" in line:
                found_coords = False
            if "Total Gradient" in line or "Total gradient" in line or "New Matrix" in line:
                found_grad = True
            if found_grad is True:
                if len(ls)==4 and isfloat(ls[1]) and isfloat(ls[2]) and isfloat(ls[3]):
                    grad.append([float(s) for s in ls[1:4]])
        if len(coords)==0:
            raise Exception("The coordinates in {0}/grad.out have length zero".format(os.getcwd()))
        if len(grad) != len(coords):
            raise Exception("The length of the coordinates in {0}/grad.out is not equal to the length of the gradients".format(os.getcwd()))
        return np.asarray(grad)

    def runCalc(self, fnm, wq, run_script):
        pre = fnm.split('.')[0]
        if wq is not None:
            if run_script is None:
                queue_up_src_dest(wq,"psi4 {0} -n {1} &> {2}.log".format(fnm, self.nt, pre),
                input_files=[(os.path.join(os.getcwd(),fnm), fnm)],
                output_files=[(os.path.join(os.getcwd(),"{0}.out".format(pre)),"{0}.out".format(pre)),
                (os.path.join(os.getcwd(),"{0}.log".format(pre)),"{0}.log".format(pre))])
            else:
                shutil.copyfile(run_script, 'run_psi.sh')
                queue_up_src_dest(wq,"sh run_psi.sh {0} &> {1}.log".format(fnm, pre),
                input_files=[(os.path.join(os.getcwd(),fnm), fnm),
                (os.path.join(os.getcwd(), "run_psi.sh"), "run_psi.sh")],
                output_files=[(os.path.join(os.getcwd(),"{0}.out".format(pre)),"{0}.out".format(pre)),
                (os.path.join(os.getcwd(),"{0}.log".format(pre)),"{0}.log".format(pre))])
        else:
            _exec("psi4 {0} -n {1} &> {2}.log".format(fnm, self.nt, pre))


class Reopt:
    """
    This class handles the main functions of the code, including the clustering. 
    """
    def __init__(self, **kwargs):
        #Get all of the user info needed for the code.

        #Need a ForceBalance directory for the targets and the MM options.
        self.fbdir = kwargs.get('fbdir')
        
        #Make work_queue object if supplied
        self.wq_port = kwargs.get('wq_port', None)
        if self.wq_port is not None: self.wq = makeWorkQueue(self.wq_port)
        else: self.wq = None
        
        #If supplied special indices for the clusters, then get the filename for this.
        #Also get the clustering algorithm and whether structures produced from a 
        #previous version of the code should be deleted.
        self.index_file = kwargs.get('custom_cluster_indices', None)
        self.cluster_alg = kwargs.get('cluster_algorithm')
        self.redo_clusters = kwargs.get('redo_clusters')
        self.default_cluster_indices = kwargs.get('default_cluster_indices')

        #If a script has been supplied that allows for the MD jobs to be submitted one 
        #by one with work_queue, then get the filename for this.
        self.remote_md = kwargs.get('remote_md', None)

        #Get options for the QM calculations.
        self.qm_engine = kwargs.get('qm_engine')
        self.qm_method = kwargs.get('qm_method')
        self.cbs = kwargs.get('cbs')
        self.basis = kwargs.get('basis')
        self.grad = kwargs.get('grad')
        self.remote_qm = kwargs.get('remote_qm')
        self.nt = kwargs.get('nt')
        self.mem = kwargs.get('mem')
        self.chg = kwargs.get('charges')
        self.mu = kwargs.get('mult')

        if self.remote_qm is not None:
            self.remote_qm = os.path.abspath(self.remote_qm)

        #if self.custom_eng is not None:
        #    self.custom_eng = os.path.abspath(self.custom_eng)
	
        #Determine if energies will be plotted or not. They are plotted by default.
        self.plot = kwargs.get('plot', None)
        self.output_dir = kwargs.get('outputdir')

    def parseFBInput(self):
        #This reads through the provided ForceBalance directory and sets up
        #the MD engine and options for the rest of the code. Make sure your input
        #file contains the ForceBalance input file as a ".in" extension."
        printcool("Reading Grids")

        self.target_list = []
        self.output_list = {}
        self.md_engine_opts = {}
        f_list = os.listdir(self.fbdir)
        infile = [i for i in f_list if ".in" in i][0]

        found_target = False
        mmopt = False
        for f in os.listdir("{0}/result".format(self.fbdir)):
            for ff in os.listdir("{0}/result/{1}".format(self.fbdir,f)):
                self.ffname = "{0}/result/{1}/{2}".format(self.fbdir,f,ff)
        
        for line in open("{0}/{1}".format(self.fbdir,infile)):
            ls = line.split()
            if "$target" in line: found_target = True
            if found_target is True:
                if "name" in line:
                    target_names = ls[1:]
                if "mmopt" in line:
                    mmopt = True
                if "type" in line:
                    if "AbInitio" in line and mmopt is False:
                        self.target_list.extend(target_names[:])
                        self.md_engine_name = line.split('_')[1]
                    else:
                        mmopt = False

        #Get engine specific keywords, although some engines don't require any.
        if "GMX" in self.md_engine_name:
            for line in open("{0}/{1}".format(self.fbdir,infile)):
                ls = line.split()
                if "gmxpath" in line: self.md_engine_opts["gmxpath"] = ls[1]
                if "gmxsuffix" in line: self.md_engine_opts["gmxsuffix"] = ls[1]
        elif "OpenMM" in self.md_engine_name:
            pdb_dict = {}
            for line in open("{0}/{1}".format(self.fbdir,infile)):
                ls = line.split()
                if "name" in line: name = ls[1]
                if "pdb" in line: pdb_dict[name] = ls[1]
        else:
            raise Exception("The MD Engine in the target {0} is currently not supported.".format(self.md_engine_name))   
        self.coord_set = {}
        self.top_set = {}
        for dnm in sorted(os.listdir("{0}/targets".format(self.fbdir))):
            if dnm in self.target_list:
                if '-' in dnm: res = dnm.split('-')[0]
                elif '_' in dnm: res = dnm.split('_')[0]
                else: res = dnm
                if res not in self.coord_set:
                    self.coord_set[res] = Molecule(os.path.join("{0}/targets".format(self.fbdir), dnm, 'all.gro'))
                    if "GMX" in self.md_engine_name: self.top_set[res] = os.path.join("{0}/targets".format(self.fbdir), dnm, 'topol.top')
                    elif "OpenMM" in self.md_engine_name: self.top_set[res] = os.path.join("{0}/targets".format(self.fbdir), dnm, pdb_dict[dnm]) 
                else: self.coord_set[res] += Molecule(os.path.join("{0}/targets".format(self.fbdir), dnm, 'all.gro'))
                self.output_list[res] = self.output_dir + "/" + res

        #The purpose for the md_engine object is to handle the specific routines for 
        #each md engine
        if "GMX" in self.md_engine_name:
            self.md_engine = GMXTask(self.md_engine_opts, self.wq, self.coord_set, self.top_set, self.ffname, self.remote_md)
            self.min_file = "gmx-min.gro"
        elif "OpenMM" in self.md_engine_name:
            self.md_engine = OpenMMTask(self.md_engine_opts, self.wq, self.coord_set, self.top_set, self.ffname, self.remote_md)
            self.min_file = "omm-min.pdb"

    def minGrids(self):
        #Minimizes the grid points by calling the corresponding function
        #in the md_engine object.
        printcool("Minimizing Grid Points")

        if not os.path.exists(self.output_dir): os.makedirs(self.output_dir)
        cwd = os.getcwd()
        for res in self.coord_set:
            if not os.path.exists(self.output_list[res]): os.makedirs(self.output_list[res])
            os.chdir(self.output_list[res])
            self.md_engine.min_grids(res, cwd, self.min_file)
            os.chdir(cwd)
        if self.wq is not None: wq_wait(self.wq)

    def cluster(self):
        #This part of the code clusters the previously MM-minimized structures
        #by Scipy's linkage algorithm based off of the RMSD. Edit the fcluster
        #line fif you need more variation in your clusters.
        printcool("Clustering")

        if self.index_file is not None:
            self.indices = getClusterIndices(self.index_file, list(self.coord_set.keys()))
        else:
            self.indices = {}
        
        self.clusters = {}
        cwd = os.getcwd()
        for res in self.coord_set:
            cluster_dict = {}
            os.chdir(self.output_list[res])
            if os.path.isdir('Clusters') and self.redo_clusters is True:
                shutil.rmtree('Clusters')
                if os.path.isdir('ClusterOpt'):
                    shutil.rmtree('ClusterOpt')
            if not os.path.isdir('Clusters'):
                os.mkdir('Clusters')
                traj, M = self.md_engine.cluster(self.min_file)
                if res not in list(self.indices.keys()):
                    if self.default_cluster_indices == "heavy":
                        self.indices[res] = [a.index for a in traj.top.atoms if a.element.name != 'hydrogen']
                    elif self.default_cluster_indices == "all":
                        self.indices[res] = [a.index for a in traj.top.atoms]
                    else:
                        raise Exception("{0} is not a valid option for the default cluster indices.".format(self.default_cluster_indices))
                if self.cluster_alg == "hybrid":
                    medoids, assignments, distance_to_current = hybrid_kmedoids(traj, 0.01, self.indices[res])
                    os.chdir('./Clusters')
                    for cl in range(len(medoids)):
                        cl_list = [i for i in range(len(assignments)) if medoids[cl] == assignments[i]]
                        cluster_traj = M[cl_list]
                        cluster_traj.write("State_{0}.pdb".format(cl))
                elif self.cluster_alg == "hierarchial":
                    RMSD = md.rmsd(traj, traj, atom_indices=self.indices[res])
                    cluster = linkage(np.reshape(RMSD,(len(RMSD),1)), method="centroid")
                    clusters = fcluster(cluster, 0.001, "distance")
                    for i in range(1,max(clusters)+1): cluster_dict[i] = []
                    os.chdir('./Clusters')
                    for i in range(1,max(clusters)+1): cluster_dict[i] = []
                    for i in range(len(clusters)):
                        clusterNum = clusters[i]
                        cluster_dict[clusterNum].append(i)
                    for cl in cluster_dict:
                        clusterTraj = M[cluster_dict[cl]]
                        clusterTraj.write("State_{0}.pdb".format(cl))
                    if not res in self.clusters: self.clusters[res] = cluster_dict
                else:
                    raise Exception("Only hybrid k_medoids and hierarchial clustering are offered.")
            os.chdir(cwd)

    def clusterMinMM(self):
        #Minimize the newly formed clusters, again using the corresponding part of the 
        #code in md_engine.
        printcool("Cluster Center Minimization")
        cwd = os.getcwd()
        for res in self.coord_set:
            os.chdir(self.output_list[res])
            for pdbfnm in sorted(os.listdir('Clusters')):
                sn = int(os.path.splitext(pdbfnm)[0].split('_')[1].replace('State',''))
                cdnm = os.path.join('ClusterOpt', "{0}".format(sn))
                if not os.path.isdir(cdnm): os.makedirs(cdnm)
                os.chdir(cdnm)
                self.md_engine.cluster_min_MM(pdbfnm, cwd, cdnm, self.min_file, res)
                os.chdir('../../')
            os.chdir(cwd)
        if self.wq is not None: wq_wait(self.wq)
                
    def clusterSinglepointsMM(self):
        #Get the singlepoint energy of the MM-optmized clusters.
        printcool("Cluster Center Single Points MM")
        self.mm_energy = {}
        cwd = os.getcwd()
        for res in self.coord_set:
            self.mm_energy[res] = []
            os.chdir(self.output_list[res])
            for k in os.listdir('ClusterOpt'):
                cdnm = os.path.join('ClusterOpt', "{0}".format(k))
                os.chdir(cdnm)
                energy = self.md_engine.cluster_singlepoint_MM(self.min_file, res)
                self.mm_energy[res].append(energy/4.184)
                os.chdir('../../')
            os.chdir(cwd)
        return self.mm_energy

    def clusterSinglepointsQM(self):
        printcool("Cluster Center Single Points QM")
        if self.qm_engine=="Psi4":
            self.QM = Psi4Task(self.fbdir, self.qm_method, self.basis, self.cbs, self.grad, self.nt, self.mem)
        else:
            raise Exception("Only Psi4 is supported for QM calculations for now.")
        self.charges, self.mult = getChargesMult(self.target_list, self.chg, self.mu)
        cwd = os.getcwd()
        for res in self.coord_set:
            os.chdir(self.output_list[res])
            for cluster in os.listdir('ClusterOpt'):
                cdnm = os.path.join('ClusterOpt', "{0}".format(cluster))
                os.chdir(cdnm)
                M = Molecule(self.min_file)
                if not os.path.exists("energy.out"):
                    self.writeQMenergy(M, "energy.dat", res)
                    self.QM.runCalc("energy.dat", self.wq, self.remote_qm)
                if self.grad is True:
                    if not os.path.exists("grad.out"):
                        self.writeQMgrad(M, "grad.dat", res)
                        self.QM.runCalc("grad.dat", self.wq, self.remote_qm)
                os.chdir('../../')
            os.chdir(cwd)
        if self.wq is not None: wq_wait(self.wq)

    def readQMEng(self):
        cwd = os.getcwd()
        qm_energy = {}
        qm_coords = {}
        qm_grad = {}
        for res in self.coord_set:
            os.chdir(self.output_list[res])
            qm_energy[res] = []
            qm_coords[res] = []
            if self.grad is True:
                qm_grad[res] = []
            for k in os.listdir('ClusterOpt'):
                cdnm = os.path.join('ClusterOpt', "{0}".format(k))
                os.chdir(cdnm)
                qm_eng, elem, coords = self.QM.readEnergy("energy.out")
                qm_energy[res].append(qm_eng)
                qm_coords[res].append(np.asarray(coords))
                if self.grad is True:
                    grad = self.QM.readGrad("grad.out")
                    qm_grad[res].append(grad)
                os.chdir('../../')
            os.chdir(cwd)
        return qm_energy, qm_grad, qm_coords
    
    def writeQMenergy(self, mol, fnm, res):
        self.QM.writeEnergy(mol, fnm, self.charges[res], self.mult[res])

    def writeQMgrad(self, mol, fnm, res):
        self.QM.writeGrad(mol, fnm, self.charges[res], self.mult[res])

    def pltEnergies(self, mm_energy, qm_energy):
        if self.plot is True:
            cwd = os.getcwd()
            for res in mm_energy:
                os.chdir(self.output_list[res])

                qm = qm_energy[res]
                qm = np.asarray(qm)
                qm = qm * 627.51
                eng_min_arg = qm.argmin()
                qm -= qm[eng_min_arg]
                np.savetxt('qm_energy.txt', qm)

                mm = mm_energy[res]
                mm = np.array(mm)
                mm -= mm[eng_min_arg]
                np.savetxt('mm_energy.txt', mm)

                plt.figure(1, figsize=(6,6))
                fig, axes = plt.subplots(nrows=1, ncols=1)
                xymin = min(min(mm), min(qm))-1
                xymax = max(max(mm), max(qm))+1
                axes.set(aspect=1)
                axes.set(xlim=[xymin, xymax])
                axes.set(ylim=[xymin, xymax])
                plt.scatter(qm, mm, s=120, alpha=0.5)
                dxy = 1.0
                plt.plot(np.arange(xymin, xymax, dxy*0.01), np.arange(xymin, xymax, dxy*0.01), color='k', alpha=0.4, lw=5)
                plt.xlabel('QM energy')
                plt.ylabel('MM energy')
                plt.title('{0} QM vs. MM optimized energies'.format(res))
                plt.savefig('{0}_cluster.pdf'.format(res))
                os.chdir(cwd)
            
    def makeNewTargets(self, qm_energy, qm_coords, qm_grad):
        #Output the new data as a ForceBalance target.
        home = os.getcwd()
        if not os.path.isdir('{0}/new_targets'.format(self.output_dir)): os.mkdir('{0}/new_targets'.format(self.output_dir))
        os.chdir('{0}/new_targets'.format(self.output_dir))
        cwd = os.getcwd()
        for res in self.coord_set:
            if not os.path.isdir('{0}-mmopt'.format(res)): os.mkdir('{0}-mmopt'.format(res))
            os.chdir('{0}-mmopt'.format(res))
            coords = qm_coords[res]
            mol = self.coord_set[res]
            mol.xyzs = coords
            mol.qm_energies = qm_energy[res]
            if self.grad is True:
                mol.qm_grads = qm_grad[res]
            mol.comms = ["MM-optimized cluster center {0}".format(i) for i in range(len(qm_coords[res]))]
            mol.boxes = [mol.boxes[0] for i in range(len(qm_coords[res]))]
            mol.write('all.gro')
            mol.write('qdata.txt')
            if "GMX" in self.md_engine_name:
                shutil.copy('{0}/{1}'.format(home,self.top_set[res]), '.')
            elif "OpenMM" in self.md_engine_name:
                shutil.copy('{0}/{1}'.format(home,self.top_set[res]), '.')
            os.chdir(cwd)

def run_reopt(**kwargs):
    reopt = Reopt(**kwargs)
    reopt.parseFBInput()
    reopt.minGrids()
    reopt.cluster()
    reopt.clusterMinMM()
    mm_energy = reopt.clusterSinglepointsMM()
    reopt.clusterSinglepointsQM()
    qm_energy, qm_grad, qm_coords = reopt.readQMEng()
    reopt.pltEnergies(mm_energy, qm_energy)
    reopt.makeNewTargets(qm_energy, qm_coords, qm_grad)

def main():
    parser = argparse.ArgumentParser(description="For use in conjunction with ForceBalance to identify spurious minima in the MM potential energy surface. The outputs from this program can then be added as ForceBalance targets for another optimization cycle. Currently works with OpenMM and GROMACS targets, with Psi4 as the QM engine.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('fbdir', type=str, help="The name of the ForceBalance directory containing all of the needed data.")
    parser.add_argument('--outputdir', type=str, default='reopt_result', help="The name of the output directory for the calculation.")
    parser.add_argument('--plot', default=True, action='store_false', help="If true, will make a final plot of the QM vs. MM energies")
    parser.add_argument('--cluster_algorithm', type=str, default='hybrid', help="Choose the clustering algorithm. Default is hybrid k medoids, eneter 'hierarchial' for hierarchial clustering.")
    parser.add_argument('--custom_cluster_indices', type=str, default=None, help="If custom indices are desired for the clustering, then provide a file with the desired indices for the target you want. An example file is provided in the ForceBalance tools directory.")
    parser.add_argument('--default_cluster_indices', type=str, default='heavy', help="The default clustering code will only pick out heavy atoms. Write 'all' if you want all indices to be used.")
    parser.add_argument('--redo_clusters', default=False, action="store_true", help="If true, then the cluster definitions you provided to the code can be changed without having to redo the whole code. Either provide a new collection of indices or use the program's default")
    parser.add_argument('--wq_port', type=int, default=None, help='Specify port number to use Work Queue to distribute optimization jobs.')
    parser.add_argument('--remote_md', type=str, default=None, help="You can specify a file that will allow for the work queue computing algorithms to run multiple MD jobs at once. An example file is included in the Examples directory.")
    parser.add_argument('--remote_qm', type=str, default=None, help="If you need to load specific environment variables for your QM program, you can submit a QM script that will load these options.")
    parser.add_argument('--qm_engine', type=str, default='Psi4', help="Enter which program you want to use for the QM calculations. Currently this only works with Psi4, but you can alter the existing Psi4 code template to match whichever program you would like to use.")
    parser.add_argument('--qm_method', type=str, default="MP2", help="Identify which QM method you would like to use for the energies and gradients. The only option currently is MP2.")
    parser.add_argument('--basis', type=str, default='aug-cc-pVTZ', help="Identify which basis set you would like to use. aug-cc-pVTZ is the default.")
    parser.add_argument('--cbs', action="store_true", default=False, help="If true, will do a cbs energy calculation.")
    parser.add_argument('--grad', default=True, action='store_false', help="If true, will use the QM program to compute the gradients on each molecule.")
    parser.add_argument('--charges', type=str, default=None, nargs='+', help="If the charges for your system is nonzero, enter the residue/system name followed by the charge. This information will be supplied to the QM calculation. The default value for the charge will be 0.")
    parser.add_argument('--mult', type=str, default=None, nargs='+', help="If the multiplicity for your system is not 1, enter the residue/system name followed by the multiplicity. This information will be supplied to the QM calculation. The default value for the multiplicity will be 1.")
    parser.add_argument('--nt', type=int, default=1, help="Specify the number of threads for the QM calculations.")
    parser.add_argument('--mem', type=int, default=4, help="Specify the memory your calculations will need. The default should generally be sufficient.")

    args = parser.parse_args()
    run_reopt(**vars(args))

if __name__ == "__main__":
    main()
