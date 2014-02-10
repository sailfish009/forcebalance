""" @package forcebalance.optimizer Optimization algorithms.

My current implementation is to have a single optimizer class with several methods
contained inside.

@author Lee-Ping Wang
@date 12/2011

"""

import os, pickle, re, sys
import numpy as np
from copy import deepcopy
import forcebalance
from forcebalance.nifty import col, flat, row, printcool, printcool_dictionary, pvec1d, pmat2d, warn_press_key, invert_svd, wopen, bak
from forcebalance.finite_difference import f1d7p, f1d5p, fdwrap
from collections import OrderedDict
import random
from forcebalance.output import getLogger, DEBUG, CleanStreamHandler
logger = getLogger(__name__)

# Global variable corresponding to the iteration number.
ITERATION_NUMBER = 0
# Global variable corresponding to whether the optimization took a good step.
GOODSTEP = 0

def Counter():
    global ITERATION_NUMBER
    return ITERATION_NUMBER

def GoodStep():
    global GOODSTEP
    return GOODSTEP

class Optimizer(forcebalance.BaseClass):
    """ Optimizer class.  Contains several methods for numerical optimization.

    For various reasons, the optimizer depends on the force field and fitting
    targets (i.e. we cannot treat it as a fully independent numerical optimizer).
    The dependency is rather weak which suggests that I can remove it someday.
    """
        
    def __init__(self,options,Objective,FF):
        """ Create an Optimizer object.
        
        The optimizer depends on both the FF and the fitting targets so there
        is a chain of dependencies: FF --> FitSim --> Optimizer, and FF --> Optimizer
        
        Here's what we do:
        - Take options from the parser
        - Pass in the objective function, force field, all fitting targets

        """
        super(Optimizer, self).__init__(options)

        ## A list of all the things we can ask the optimizer to do.
        self.OptTab    = {'NEWTONRAPHSON'     : self.NewtonRaphson, 
                          'OPTIMIZE'          : self.NewtonRaphson, 
                          'NEWTON'            : self.NewtonRaphson, 
                          'NR'                : self.NewtonRaphson, 
                          'BFGS'              : self.BFGS,
                          'SCIPY_BFGS'        : self.Scipy_BFGS,
                          'POWELL'            : self.Powell,
                          'SIMPLEX'           : self.Simplex,
                          'ANNEAL'            : self.Anneal,
                          'BASIN'             : self.BasinHopping,
                          'BASINHOPPING'      : self.BasinHopping,
                          'GENETIC'           : self.GeneticAlgorithm,
                          'CG'                : self.ConjugateGradient,
                          'CONJUGATEGRADIENT' : self.ConjugateGradient,
                          'TNC'               : self.TruncatedNewton,
                          'NCG'               : self.NewtonCG,
                          'SCAN_MVALS'        : self.ScanMVals,
                          'SCAN_PVALS'        : self.ScanPVals,
                          'SINGLE'            : self.SinglePoint,
                          'GRADIENT'          : self.Gradient,
                          'HESSIAN'           : self.Hessian,
                          'FDCHECKG'          : self.FDCheckG,
                          'FDCHECKH'          : self.FDCheckH
                          }
        
        #======================================#
        # Options that are given by the parser #
        #======================================#
        ## The root directory
        self.set_option(options,'root','root')
        ## The job type
        self.set_option(options,'jobtype','jobtype')
        ## Initial step size trust radius
        self.set_option(options,'trust0','trust0')
        ## Minimum trust radius (for noisy objective functions)
        self.set_option(options,'mintrust','mintrust')
        ## Lower bound on Hessian eigenvalue (below this, we add in steepest descent)
        self.set_option(options,'eig_lowerbound','eps')
        ## Lower bound on step size (will fail below this)
        self.set_option(options,'step_lowerbound')
        ## Guess value for Brent
        self.set_option(options,'lm_guess','lmg')
        ## Step size for numerical finite difference
        self.set_option(options,'finite_difference_h','h')
        self.set_option(options,'finite_difference_h','h0')
        ## When the trust radius get smaller, the finite difference step might need to get smaller.
        self.set_option(options,'finite_difference_factor','fdf')
        ## Number of steps to average over
        self.set_option(options,'objective_history','hist')
        ## Function value convergence threshold
        self.set_option(options,'convergence_objective')
        ## Step size convergence threshold
        self.set_option(options,'convergence_step')
        ## Gradient convergence threshold
        self.set_option(options,'convergence_gradient')
        ## Allow convergence on low quality steps
        self.set_option(options,'converge_lowq')
        ## Maximum number of optimization steps
        self.set_option(options,'maxstep','maxstep')
        ## For scan[mp]vals: The parameter index to scan over
        self.set_option(options,'scanindex_num','idxnum')
        ## For scan[mp]vals: The parameter name to scan over, it just looks up an index
        self.set_option(options,'scanindex_name','idxname')
        ## For scan[mp]vals: The values that are fed into the scanner
        self.set_option(options,'scan_vals','scan_vals')
        ## Name of the checkpoint file that we're reading in
        self.set_option(options,'readchk','rchk_fnm')
        ## Name of the checkpoint file that we're writing out
        self.set_option(options,'writechk','wchk_fnm')
        ## Whether to write the checkpoint file at every step
        self.set_option(options,'writechk_step','wchk_step')
        ## Adaptive trust radius adjustment factor
        self.set_option(options,'adaptive_factor','adapt_fac')
        ## Adaptive trust radius adjustment damping
        self.set_option(options,'adaptive_damping','adapt_damp')
        ## Whether to print gradient during each step of the optimization
        self.set_option(options,'print_gradient','print_grad')
        ## Whether to print Hessian during each step of the optimization
        self.set_option(options,'print_hessian','print_hess')
        ## Whether to print parameters during each step of the optimization
        self.set_option(options,'print_parameters','print_vals')
        ## Error tolerance (if objective function rises by less than this, then the optimizer will forge ahead!)
        self.set_option(options,'error_tolerance','err_tol')
        ## Search tolerance (The Hessian diagonal search will stop if the change is below this threshold)
        self.set_option(options,'search_tolerance','search_tol')
        self.set_option(options,'read_mvals')
        self.set_option(options,'read_pvals')
        ## Whether to make backup files
        self.set_option(options, 'backup')
        ## Name of the original input file
        self.set_option(options, 'input_file')
        ## Number of convergence criteria that must be met
        self.set_option(options, 'criteria')
        ## Only backup the "mvals" input file once per calculation.
        self.mvals_bak = 1
        ## Print a special message on failure.
        self.failmsg = 0
        
        #======================================#
        #     Variables which are set here     #
        #======================================#
        ## The objective function (needs to pass in when I instantiate)
        self.Objective = Objective
        ## Whether the penalty function is hyperbolic
        self.bhyp      = Objective.Penalty.ptyp != 2
        ## The force field itself
        self.FF        = FF
        ## Re-evaluate the objective function when an optimization step is rejected
        self.set_option(options, 'reevaluate', default=any(['liquid' or 'lipid' in tgt.type.lower() for tgt in self.Objective.Targets]), forceprint=True)
        self.bakdir    = os.path.join(os.path.splitext(options['input_file'])[0]+'.bak')
        self.resdir    = os.path.join('result',os.path.splitext(options['input_file'])[0])
        
        #======================================#
        #    Variables from the force field    #
        #======================================#
        ## The indices to be excluded from the Hessian update
        self.excision  = list(FF.excision)

        ## Number of parameters
        self.np        = FF.np
        ## The original parameter values
        if options['read_mvals'] != None:
            self.mvals0    = np.array(options['read_mvals'])
        elif options['read_pvals'] != None:
            self.mvals0    = FF.create_mvals(options['read_pvals'])
        else:
            self.mvals0    = np.zeros(self.FF.np)

        ## Print the optimizer options.
        printcool_dictionary(self.PrintOptionDict, title="Setup for optimizer")
        ## Load the checkpoint file.
        self.readchk()

    def save_mvals_to_input(self, mvals):
        """ Write a new input file (%s_save.in) containing the current mathematical parameters. """
        ## Determine the output file name.
        base, ext = os.path.splitext(self.input_file)
        if not base.endswith(".sav"):
            outfnm = base+".sav"
        else:
            outfnm = base+ext
        ## Clone the input file to the output, 
        if self.input_file != None and os.path.exists(self.input_file):
            fin = open(self.input_file).readlines()
            have_mvals = 0
            in_mvals = 0
            in_options = 0
            if os.path.exists(outfnm) and self.mvals_bak: 
                bak(outfnm, dest=self.bakdir)
            self.mvals_bak = 0
            fout = open(outfnm, 'w')
            for line in fin:
                line1 = line.split("#")[0].strip().lower()
                if line1.startswith("$options"):
                    in_options = 1
                if in_options and line1.startswith("$end"):
                    if not have_mvals:
                        print >> fout, "read_mvals"
                        print >> fout, self.FF.sprint_map(mvals, precision=8)
                        print >> fout, "/read_mvals"
                        have_mvals = 1
                    in_options = 0
                if line1.startswith("/read_mvals"):
                    in_mvals = 0
                if in_mvals: continue
                print >> fout, line,
                if line1.startswith("read_mvals"):
                    if have_mvals: raise RuntimeError("Encountered more than one read_mvals section")
                    have_mvals = 1
                    in_mvals = 1
                    print >> fout, self.FF.sprint_map(mvals, precision=8)
        return outfnm
            
    def Run(self):
        """ Call the appropriate optimizer.  This is the method we might want to call from an executable. """

        xk = self.OptTab[self.jobtype]()

        ## Don't print a "result" force field if it's the same as the input.
        print_parameters = True
        if xk == None and (self.mvals0 == np.zeros(self.FF.np)).all(): 
            logger.info("Parameter file same as original; will not be printed to results folder.\n")
            print_parameters = False
        elif xk == None:
            xk = self.mvals0

        ## Check derivatives by finite difference after the optimization is over (for good measure)
        check_after = False
        if check_after:
            self.mvals0 = xk.copy()
            self.FDCheckG()

        ## Print out final answer
        if print_parameters:
            bar = printcool("Final optimization parameters:",color=4)
            self.FF.print_map(xk)
            bar = printcool("Final physical parameters:",color=4)
            self.FF.print_map(self.FF.create_pvals(xk))
            logger.info(bar)
            if self.backup:
                for fnm in self.FF.fnms:
                    if os.path.exists(os.path.join(self.resdir, fnm)):
                        bak(os.path.join(self.resdir, fnm))
            self.FF.make(xk,printdir=self.resdir)
            # logger.info("The force field has been written to the '%s' directory.\n" % self.resdir)
            outfnm = self.save_mvals_to_input(xk)
            # logger.info("Input file with optimization parameters saved to %s.\n" % outfnm)
            printcool("The force field has been written to the %s directory.\n"
                      "Input file with optimization parameters saved to %s." % (self.resdir, outfnm), color=0)
                      # "To reload these parameters, use %s as the input\n"
                      # "file without changing the '%s' directory." % 
                      # (outfnm, outfnm, self.FF.ffdir), color=0, center=False, sym2='-')

        ## Write out stuff to checkpoint file
        self.writechk()

        ## Print out final message
        if self.failmsg:
            bar = printcool("It is hard to fail, but it is worse\nnever to have tried to succeed.",ansi="40;97")
        else:
            bar = printcool("Calculation Finished.\n---==(  May the Force be with you!  )==---",ansi="1;44;93")

        return xk

    def adjh(self, trust):
        # The finite difference step size should be at most 1% of the trust radius.
        h = min(self.fdf*trust, self.h0)
        if h != self.h:
            logger.info("Setting finite difference step to %.4e\n" % h)
            self.h = h
            for tgt in self.Objective.Targets:
                tgt.h = h
            
    def MainOptimizer(self,b_BFGS=0):
        """ 

        The main ForceBalance adaptive trust-radius pseudo-Newton
        optimizer.  Tried and true in many situations. :)

        Usually this function is called with the BFGS or NewtonRaphson
        method.  The NewtonRaphson method is consistently the best
        method I have, because I always provide at least an
        approximate Hessian to the objective function.  The BFGS
        method works well, but if gradients are cheap the SciPy_BFGS
        method also works nicely.

        The method adaptively changes the step size.  If the step is
        sufficiently good (i.e. the objective function goes down by a
        large fraction of the predicted decrease), then the step size
        is increased; if the step is bad, then it rejects the step and
        tries again.

        The optimization is terminated after either a function value or
        step size tolerance is reached.

        @param[in] b_BFGS Switch to use BFGS (True) or Newton-Raphson (False)

        """

        if self.trust0 < 0.0:
            detail = "(Hessian Diagonal Search)"
        elif self.adapt_fac != 0.0:
            detail = "(Adaptive Trust Radius)"
        else:
            detail = "(Trust Radius)"
        printcool("Main Optimizer \n%s Method %s\n\n"
                  "\x1b[0mConvergence criteria (%i of 3 needed):\n"
                  "\x1b[0mObjective Function  : %.3e\n"
                  "\x1b[0mNorm of Gradient    : %.3e\n"
                  "\x1b[0mParameter step size : %.3e" % 
                  ("BFGS" if b_BFGS else "Newton-Raphson", detail, self.criteria,
                   self.convergence_objective, self.convergence_gradient, 
                   self.convergence_step), ansi=1, bold=1)

        # Optimization contains uncertainty.
        Uncertainty = any(['liquid' or 'lipid' in tgt.type.lower() for tgt in self.Objective.Targets])

        # Print a warning if optimization is unlikely to converge
        if Uncertainty and self.convergence_objective < 1e-3:
            warn_press_key("Condensed phase targets detected - may not converge with current choice of"
                           " convergence_objective (%.e)\nRecommended range is 1e-2 - 1e-1 for this option." % self.convergence_objective)

        #========================#
        #| Initialize variables |#
        #========================#
        # Order of derivatives
        Ord         = 1 if b_BFGS else 2
        # Iteration number counter.
        global ITERATION_NUMBER
        ITERATION_NUMBER = 0
        # Indicates if the optimization step was "good" (i.e. not rejected).
        global GOODSTEP
        GOODSTEP = 1
        # Indicates if the optimization is currently at the lowest value of the objective function so far.
        Best_Step = 1
        # Objective function history.
        X_hist    = np.array([])
        # Trust radius.
        trust     = abs(self.trust0)
        # Current value of the parameters.
        xk        = self.mvals0.copy()
        # The current optimization step.
        dx        = np.zeros(self.FF.np)
        # Length of the current optimization step.
        ndx       = 0.0
        # Color indicating the quality of the optimization step.
        color     = "\x1b[1m"
        # Ratio of actual objective function change to expected change.
        Quality   = 0.0
        # Threshold for "low quality step" which decreases trust radius.
        ThreLQ = 0.25
        # Threshold for "high quality step" which increases trust radius.
        ThreHQ = 0.75
        printcool("Color Key for Objective Function -=X2=-\n\x1b[1mBold\x1b[0m = Initial step\n"
                  "\x1b[92mGreen = Current lowest value of objective function%s\x1b[0m\n"
                  "\x1b[91mRed = Objective function rises, step rejected\x1b[0m\n"
                  "\x1b[0mNo color = Not at the lowest value" % " (best estimate)" if Uncertainty else "", \
                      bold=0, color=0, center=[True, False, False, False, False])
        # Optimization steps before this one are ineligible for consideration for "best step".
        Best_Start = 0

        def print_progress(itn, nx, nd, ng, clr, x, std, qual):
            # Step number, norm of parameter vector / step / gradient, objective function value, change from previous steps, step quality.
            logger.info("\n")
            logger.info("%6s%12s%12s%12s%14s%12s%12s\n" % ("Step", "  |k|  ","  |dk|  "," |grad| ","    -=X2=-  ","Delta(X2)", "StepQual"))
            logger.info("%6i%12.3e%12.3e%12.3e%s%14.5e\x1b[0m%12.3e% 11.3f\n\n" % (itn, nx, nd, ng, clr, x, std, qual))

        #=====================================#
        #|       Nonlinear Iterations        |#
        #| Loop until convergence is reached |#
        #=====================================#
        while 1:
            #================================#
            #| Evaluate objective function. |#
            #================================#
            if len(self.chk.keys()) > 0 and ITERATION_NUMBER == 0:
                printcool("Iteration %i: Reading initial objective, gradient, Hessian from checkpoint file" % (ITERATION_NUMBER), color=4, bold=0)
                logger.info("Reading initial objective, gradient, Hessian from checkpoint file\n")
                xk, X, G, H   = self.chk['xk'], self.chk['X'], self.chk['G'], self.chk['H']
                X_hist, trust = self.chk['X_hist'], self.chk['trust']
            else:
                self.adjh(trust)
                printcool("Iteration %i: Evaluating objective function\nand derivatives through %s order" % (ITERATION_NUMBER, "first" if Ord == 1 else "second"), color=4, bold=0)
                data        = self.Objective.Full(xk,Ord,verbose=True)
                X, G, H = data['X'], data['G'], data['H']
            trustprint = ''
            #================================#
            #|   Assess optimization step.  |#
            #================================#
            if ITERATION_NUMBER > 0:
                dX_actual = X - X_prev
                Best_Step = X < np.min(X_hist[Best_Start:])
                try:
                    Quality = dX_actual / dX_expect
                except:
                    # This should only be encountered in the Hessian diagonal search code (i.e. trust0 < 0).
                    logger.warning("Warning: Step size of zero detected (i.e. wrong direction). "
                                   "Try reducing the finite_difference_h parameter\n")
                    Quality = 0.0
                if X > (X_prev + max(self.err_tol, self.convergence_objective)):
                    #================================#
                    #|        Reject step if        |#
                    #|  objective function rises.   |#
                    #================================#
                    GOODSTEP = 0
                    print_progress(ITERATION_NUMBER, nxk, ndx, ngd, "\x1b[91m", X, X-X_prev, Quality)
                    xk = xk_prev.copy()
                    trust = max(ndx*(1./(1+self.adapt_fac)), self.mintrust)
                    trustprint = "Reducing trust radius to % .4e\n" % trust
                    if self.reevaluate:
                        #================================#
                        #|  Re-evaluate the objective   |#
                        #|  function and gradients at   |#
                        #|   the previous parameters.   |#
                        #================================#
                        printcool("Objective function rises!\nRe-evaluating at the previous point..",color=1)
                        ITERATION_NUMBER += 1
                        Best_Start = ITERATION_NUMBER
                        Best_Step = 1
                        self.adjh(trust)
                        X_hist = np.append(X_hist, X)
                        data        = self.Objective.Full(xk,Ord,verbose=True)
                        GOODSTEP = 1
                        X, G, H = data['X'], data['G'], data['H']
                        ndx = 0
                        nxk = np.linalg.norm(xk)
                        ngd = np.linalg.norm(G)
                        Quality = 0.0
                        color = "\x1b[92m"
                    else:
                        #================================#
                        #| Go back to the start of loop |#
                        #|    and take a reduced step.  |#
                        #================================#
                        printcool("Objective function rises!\nTaking another step from previous point..",color=1)
                        X = X_prev
                        G = G_prev.copy()
                        H = H_stor.copy()
                        data = deepcopy(datastor)
                else:
                    GOODSTEP = 1
                    #================================#
                    #|   Adjust step size based on  |#
                    #|         step quality.        |#
                    #================================#
                    if Quality <= ThreLQ and self.trust0 > 0:
                        trust = max(ndx*(1./(1+self.adapt_fac)), self.mintrust)
                        trustprint = "Low quality step, reducing trust radius to % .4e\n" % trust
                    elif Quality >= ThreHQ and bump and self.trust0 > 0:
                        trust += self.adapt_fac*trust*np.exp(-1*self.adapt_damp*(trust/self.trust0 - 1))
                        trustprint = "Increasing trust radius to % .4e\n" % trust
                    color = "\x1b[92m" if Best_Step else "\x1b[0m"
                    #================================#
                    #|  Hessian update for BFGS.    |#
                    #================================#
                    if b_BFGS:
                        Hnew = H_stor.copy()
                        Dx   = col(xk - xk_prev)
                        Dy   = col(G  - G_prev)
                        Mat1 = (Dy*Dy.T)/(Dy.T*Dx)[0,0]
                        Mat2 = ((Hnew*Dx)*(Hnew*Dx).T)/(Dx.T*Hnew*Dx)[0,0]
                        Hnew += Mat1-Mat2
                        H = Hnew.copy()
                        data['H'] = H.copy()
                    # (Experimental): Deleting lines in the parameter file
                    if len(self.FF.prmdestroy_this) > 0:
                        self.FF.prmdestroy_save.append(self.FF.prmdestroy_this)
                        self.FF.linedestroy_save.append(self.FF.linedestroy_this) 
            # Update objective function history.
            X_hist = np.append(X_hist, X)
            # Take the stdev over the previous (hist) values.
            # Multiply by 2, so when hist=2 this is simply the difference.
            stdfront = np.std(X_hist[-self.hist:]) if len(X_hist) > self.hist else np.std(X_hist)
            stdfront *= 2
            #================================#
            #| Print optimization progress. |#
            #================================#
            nxk = np.linalg.norm(xk)
            ngd = np.linalg.norm(G)
            if GOODSTEP:
                print_progress(ITERATION_NUMBER, nxk, ndx, ngd, color, X, -1*stdfront, Quality)
            #================================#
            #|   Print objective function,  |#
            #|     gradient and Hessian.    |#
            #================================#
            if self.print_grad:
                bar = printcool("Total Gradient",color=4)
                self.FF.print_map(vals=G,precision=8)
                logger.info(bar)
            if self.print_hess:
                bar = printcool("Total Hessian",color=4)
                pmat2d(H,precision=8)
                logger.info(bar)
            for key, val in self.Objective.ObjDict.items():
                if Best_Step:
                    self.Objective.ObjDict_Last[key] = val
            #================================#
            #|  Check convergence criteria. |#
            #================================#
            ncrit = 0
            if ngd < self.convergence_gradient and Best_Step:
                logger.info("Convergence criterion reached for gradient norm (%.2e)\n" % self.convergence_gradient)
                ncrit += 1
            if ndx < self.convergence_step and (self.converge_lowq or Quality > ThreLQ) and Best_Step:
                logger.info("Convergence criterion reached in step size (%.2e)\n" % self.convergence_step)
                ncrit += 1
            if stdfront < self.convergence_objective and (self.converge_lowq or Quality > ThreLQ) and len(X_hist) >= self.hist and Best_Step:
                logger.info("Convergence criterion reached for objective function (%.2e)\n" % self.convergence_objective)
                ncrit += 1
            if ncrit >= self.criteria: break
            #================================#
            #| Save optimization variables  |#
            #| before taking the next step. |#
            #================================#
            # Previous data from objective function call.
            datastor = deepcopy(data)
            # Previous objective function and derivatives
            X_prev  = X
            G_prev  = G.copy()
            H_stor  = H.copy()
            # Previous optimization variables
            xk_prev = xk.copy()
            # Previous physical parameters
            pk_prev = self.FF.create_pvals(xk)
            #================================#
            #| Calculate optimization step. |#
            #|  Increase iteration number.  |#
            #================================#
            logger.info(trustprint)
            logger.info("Calculating nonlinear optimization step\n")
            # Calculate the optimization step.
            dx, dX_expect, bump = self.step(xk, data, trust)
            # Increment the parameters.
            xk += dx
            ndx = np.linalg.norm(dx)
            # Increment the iteration counter.
            ITERATION_NUMBER += 1
            # The search code benefits from knowing the step size here.
            if self.trust0 < 0:
                trust = ndx
            # Print parameter values.
            if self.print_vals:
                pk = self.FF.create_pvals(xk)
                dp = pk - pk_prev
                bar = printcool("Mathematical Parameters (Current + Step = Next)",color=5)
                self.FF.print_map(vals=["% .4e %s %.4e = % .4e" % (xk_prev[i], '+' if dx[i] >= 0 else '-', abs(dx[i]), xk[i]) for i in range(len(xk))])
                logger.info(bar)
                bar = printcool("Physical Parameters (Current + Step = Next)",color=5)
                self.FF.print_map(vals=["% .4e %s %.4e = % .4e" % (pk_prev[i], '+' if dp[i] >= 0 else '-', abs(dp[i]), pk[i]) for i in range(len(pk))])
                logger.info(bar)
            # Write checkpoint file.
            self.chk = {'xk': xk, 'X' : X, 'G' : G, 'H': H, 'X_hist': X_hist, 'trust': trust}
            if self.wchk_step:
                self.writechk()           
            outfnm = self.save_mvals_to_input(xk)
            logger.info("Input file with saved parameters: %s\n" % outfnm)
            # Check for whether the maximum number of optimization cycles is reached.
            if ITERATION_NUMBER == self.maxstep:
                logger.info("Maximum number of optimization steps reached (%i)\n" % ITERATION_NUMBER)
                break
            # Check for whether the step size is too small to continue.
            if ndx < self.step_lowerbound:
                logger.info("Step size is too small to continue (%.3e < %.3e)\n" % (ndx, self.step_lowerbound))
                break

        cnvgd = ncrit >= self.criteria
        bar = printcool("\x1b[0m%s\x1b[0m\nFinal objective function value\nFull: % .6e  Un-penalized: % .6e" % 
                        ("\x1b[1mOptimization Converged" if cnvgd else "\x1b[1;91mConvergence Failure",
                         data['X'],data['X0']), color=2)
        self.failmsg = not cnvgd
        return xk

    def step(self, xk, data, trust):
        """ Computes the next step in the parameter space.  There are lots of tricks here that I will document later.

        @param[in] G The gradient
        @param[in] H The Hessian
        @param[in] trust The trust radius
        
        """
        from scipy import optimize

        X, G, H = (data['X0'], data['G0'], data['H0']) if self.bhyp else (data['X'], data['G'], data['H'])
        H1 = H.copy()
        H1 = np.delete(H1, self.excision, axis=0)
        H1 = np.delete(H1, self.excision, axis=1)
        Eig = np.linalg.eig(H1)[0]            # Diagonalize Hessian
        Emin = min(Eig)
        if Emin < self.eps:         # Mix in SD step if Hessian minimum eigenvalue is negative
            # Experiment.
            Adj = max(self.eps, 0.01*abs(Emin)) - Emin
            logger.info("Hessian has a small or negative eigenvalue (%.1e), mixing in some steepest descent (%.1e) to correct this.\n" % (Emin, Adj))
            logger.info("Eigenvalues are:\n")
            pvec1d(Eig)
            H += Adj*np.eye(H.shape[0])

        if self.bhyp:
            G = np.delete(G, self.excision)
            H = np.delete(H, self.excision, axis=0)
            H = np.delete(H, self.excision, axis=1)
            xkd = np.delete(xk, self.excision)
            if self.Objective.Penalty.fmul != 0.0:
                warn_press_key("Using the multiplicative hyperbolic penalty is discouraged!")
            # This is the gradient and Hessian without the contributions from the hyperbolic constraint.
            Obj0 = {'X':X,'G':G,'H':H}
            class Hyper(object):
                def __init__(self, HL, Penalty):
                    self.H = HL.copy()
                    self.dx = 1e10 * np.ones(len(HL))
                    self.Val = 0
                    self.Grad = np.zeros(len(HL))
                    self.Hess = np.zeros((len(HL),len(HL)))
                    self.Penalty = Penalty
                def _compute(self, dx):
                    self.dx = dx.copy()
                    Tmp = np.matrix(self.H)*col(dx)
                    Reg_Term   = self.Penalty.compute(xkd+flat(dx), Obj0)
                    self.Val   = (X + np.dot(dx, G) + 0.5*row(dx)*Tmp + Reg_Term[0] - data['X'])[0,0]
                    self.Grad  = flat(col(G) + Tmp) + Reg_Term[1]
                def compute_val(self, dx):
                    if np.linalg.norm(dx - self.dx) > 1e-8:
                        self._compute(dx)
                    return self.Val
                def compute_grad(self, dx):
                    if np.linalg.norm(dx - self.dx) > 1e-8:
                        self._compute(dx)
                    return self.Grad
                def compute_hess(self, dx):
                    if np.linalg.norm(dx - self.dx) > 1e-8:
                        self._compute(dx)
                    return self.Hess
            def hyper_solver(L):
                dx0 = np.zeros(len(xkd))
                HL = H + (L-1)**2*np.eye(len(H))
                HYP = Hyper(HL, self.Objective.Penalty)
                try:
                    Opt1 = optimize.fmin_bfgs(HYP.compute_val,dx0,fprime=HYP.compute_grad,gtol=1e-5,full_output=True,disp=0)
                except:
                    Opt1 = optimize.fmin(HYP.compute_val,dx0,full_output=True,disp=0)
                try:
                    Opt2 = optimize.fmin_bfgs(HYP.compute_val,-xkd,fprime=HYP.compute_grad,gtol=1e-5,full_output=True,disp=0)
                except:
                    Opt2 = optimize.fmin(HYP.compute_val,-xkd,full_output=True,disp=0)
                dx1, sol1 = Opt1[0], Opt1[1]
                dx2, sol2 = Opt2[0], Opt2[1]
                dxb, sol = (dx1, sol1) if sol1 <= sol2 else (dx2, sol2)
                for i in self.excision:    # Reinsert deleted coordinates - don't take a step in those directions
                    dxb = np.insert(dxb, i, 0)
                return dxb, sol
        else:
            # G0 and H0 are used for determining the expected function change.
            G0 = G.copy()
            H0 = H.copy()
            G = np.delete(G, self.excision)
            H = np.delete(H, self.excision, axis=0)
            H = np.delete(H, self.excision, axis=1)
            
            logger.debug("Inverting Hessian:\n")
            logger.debug(" G:\n")
            pvec1d(G,precision=5, loglevel=DEBUG)
            logger.debug(" H:\n")
            pmat2d(H,precision=5, loglevel=DEBUG)
            
            Hi = invert_svd(np.matrix(H))
            dx = flat(-1 * Hi * col(G))
            
            logger.debug(" dx:\n")
            pvec1d(dx,precision=5, loglevel=DEBUG)
            
            for i in self.excision:    # Reinsert deleted coordinates - don't take a step in those directions
                dx = np.insert(dx, i, 0)

            def para_solver(L):
                # Levenberg-Marquardt
                # HT = H + (L-1)**2*np.diag(np.diag(H))
                # Attempt to use plain Levenberg
                HT = H + (L-1)**2*np.eye(len(H))
                logger.debug("Inverting Scaled Hessian:\n")
                logger.debug(" G:\n")
                pvec1d(G,precision=5, loglevel=DEBUG)
                logger.debug(" HT: (Scal = %.4f)\n" % (1+(L-1)**2))
                pmat2d(HT,precision=5, loglevel=DEBUG)
                Hi = invert_svd(np.matrix(HT))
                dx = flat(-1 * Hi * col(G))
                logger.debug(" dx:\n")
                pvec1d(dx,precision=5, loglevel=DEBUG)
                sol = flat(0.5*row(dx)*np.matrix(H)*col(dx))[0] + np.dot(dx,G)
                for i in self.excision:    # Reinsert deleted coordinates - don't take a step in those directions
                    dx = np.insert(dx, i, 0)
                return dx, sol
    
        def solver(L):
            return hyper_solver(L) if self.bhyp else para_solver(L)
    
        def trust_fun(L):
            N = np.linalg.norm(solver(L)[0])
            logger.debug("\rL = %.4e, Hessian diagonal addition = %.4e: found length %.4e, objective is %.4e\n" % (L, (L-1)**2, N, (N - trust)**2))
            return (N - trust)**2

        def h_fun(L):
            N = np.linalg.norm(solver(L)[0])
            logger.debug("\rL = %.4e, Hessian diagonal addition = %.4e: found length %.4e, objective is %.4e\n" % (L, (L-1)**2, N, (N - trust)**2))
            return (N - self.h)**2

        def search_fun(L):
            # Evaluate ONLY the objective function.  Most useful when
            # the objective is cheap, but the derivative is expensive.
            dx, sol = solver(L) # dx is how much the step changes from the previous step.
            # This is our trial step.
            xk_ = dx + xk
            Result = self.Objective.Full(xk_,0,verbose=False)['X'] - data['X']
            logger.info("Searching! Diagonal addition = %.4e, L = % .4e, length %.4e, result % .4e\n" % ((L-1)**2,L,np.linalg.norm(dx),Result))
            return Result
        
        if self.trust0 > 0: # This is the trust region code.
            bump = False
            dx, expect = solver(1)
            dxnorm = np.linalg.norm(dx)
            if dxnorm > trust:
                bump = True
                # Tried a few optimizers here, seems like Brent works well.
                # Okay, the problem with Brent is that the tolerance is fractional.  
                # If the optimized value is zero, then it takes a lot of meaningless steps.
                LOpt = optimize.brent(trust_fun,brack=(self.lmg,self.lmg*4),tol=1e-6)
                ### Result = optimize.fmin_powell(trust_fun,3,xtol=self.search_tol,ftol=self.search_tol,full_output=1,disp=0)
                ### LOpt = Result[0]
                dx, expect = solver(LOpt)
                dxnorm = np.linalg.norm(dx)
                logger.info("Trust-radius step found (length %.4e), % .4e added to Hessian diagonal\n" % (dxnorm, (LOpt-1)**2))
            else:
                logger.info("Newton-Raphson step found (length %.4e)\n" % (dxnorm))
                
        else: # This is the search code.
            # First obtain a step that is the same length as the provided trust radius.
            LOpt = optimize.brent(trust_fun,brack=(self.lmg,self.lmg*4),tol=1e-6)
            dx, expect = solver(LOpt)
            dxnorm = np.linalg.norm(dx)
            logger.info("Starting Hessian diagonal search with step size %.4e\n" % dxnorm)
            bump = False
            Result = optimize.brent(search_fun,brack=(LOpt,LOpt*4),tol=self.search_tol,full_output=1)
            if Result[1] > 0:
                LOpt = optimize.brent(h_fun,brack=(self.lmg,self.lmg*4),tol=1e-6)
                dx, expect = solver(LOpt)
                dxnorm = np.linalg.norm(dx)
                logger.info("Restarting search with step size %.4e\n" % dxnorm)
                Result = optimize.brent(search_fun,brack=(LOpt,LOpt*4),tol=self.search_tol,full_output=1)
            ### optimize.fmin(search_fun,0,xtol=1e-8,ftol=data['X']*0.1,full_output=1,disp=0)
            ### Result = optimize.fmin_powell(search_fun,3,xtol=self.search_tol,ftol=self.search_tol,full_output=1,disp=0)
            dx, _ = solver(Result[0])
            expect = Result[1]
            logger.info("Optimization step found (length %.4e)\n" % np.linalg.norm(dx))

        ## Decide which parameters to redirect.
        ## Currently not used.
        if self.Objective.Penalty.ptyp in [3,4,5]:
            self.FF.make_redirect(dx+xk)

        return dx, expect, bump

    def NewtonRaphson(self):
        """ Optimize the force field parameters using the Newton-Raphson method (@see MainOptimizer) """
        return self.MainOptimizer(b_BFGS=0)

    def BFGS(self):
        """ Optimize the force field parameters using the BFGS method; currently the recommended choice (@see MainOptimizer) """
        return self.MainOptimizer(b_BFGS=1)

    def ScipyOptimizer(self,Algorithm="None"):
        """ Driver for SciPy optimizations.

        Using any of the SciPy optimizers requires that SciPy is installed.
        This method first defines several wrappers around the objective function that the SciPy
        optimizers can use.  Then it calls the algorith mitself.

        @param[in] Algorithm The optimization algorithm to use, for example 'powell', 'simplex' or 'anneal'

        """
        from scipy import optimize

        self.prev_bad = False
        self.xk_prev = self.mvals0.copy()
        self.x_prev = 0.0
        self.x_best = None
        
        def wrap(fin, Order=0, callback=True, fg=False):
            def fout(mvals):
                def print_heading():
                    logger.info('\n')
                    if Order == 2: logger.info("%9s%9s%12s%12s%14s%12s\n" % ("Eval(H)", "  |k|  ","  |dk|  "," |grad| ","    -=X2=-  ","Delta(X2)"))
                    elif Order == 1: logger.info("%9s%9s%12s%12s%14s%12s\n" % ("Eval(G)", "  |k|  ","  |dk|  "," |grad| ","    -=X2=-  ","Delta(X2)"))
                    else: logger.info("%9s%9s%12s%14s%12s\n" % ("Eval(X)", "  |k|  ","  |dk|  ","    -=X2=-  ","Delta(X2)"))

                def print_results(color, newline=True):
                    if newline: head = '' ; foot = '\n'
                    else: head = '\r' ; foot = '\r'
                    if Order: logger.info(head + "%6i%12.3e%12.3e%12.3e%s%14.5e\x1b[0m%12.3e" % \
                                              (fout.evals, np.linalg.norm(mvals), np.linalg.norm(mvals-self.xk_prev), np.linalg.norm(G), color, X, X - self.x_prev) + foot)
                    else: logger.info(head + "%6i%12.3e%12.3e%s%14.5e\x1b[0m%12.3e" % \
                                          (fout.evals, np.linalg.norm(mvals), np.linalg.norm(mvals-self.xk_prev), color, X, X - self.x_prev) + foot)
                Result = fin(mvals,Order=Order,verbose=False)
                fout.evals += 1
                X, G, H = [Result[i] for i in ['X','G','H']]
                if callback:
                    if X <= self.x_best or self.x_best == None:
                        color = "\x1b[92m"
                        self.x_best = X
                        self.prev_bad = False
                        if self.print_vals:
                            logger.info('\n')
                            bar = printcool("Current Mathematical Parameters:",color=5)
                            self.FF.print_map(vals=["% .4e" % i for i in mvals])
                        for Tgt in self.Objective.Targets:
                            Tgt.indicate()
                        self.Objective.Indicate()
                        print_heading()
                        print_results(color)
                    else:
                        self.prev_bad = True
                        color = "\x1b[91m"
                        if Order:
                            print_heading()
                            print_results(color)
                        else:
                            if not self.prev_bad: print_heading()
                            print_results(color, newline=False)
                    if Order:
                        if np.linalg.norm(self.xk_prev - mvals) > 0.0:
                            self.adjh(np.linalg.norm(self.xk_prev - mvals))
                    self.xk_prev = mvals.copy()
                    self.x_prev = X
                if Order == 2:
                    return H
                elif Order == 1:
                    if fg:
                        return X, G
                    else:
                        return G
                else:
                    if X != X:
                        return 1e10
                    else:
                        return X
            fout.evals = 0
            return fout

        def xwrap(func,callback=True):
            return wrap(func, Order=0, callback=callback)
        
        def fgwrap(func,callback=True):
            return wrap(func, Order=1, callback=callback, fg=True)

        def gwrap(func,callback=True):
            return wrap(func, Order=1, callback=callback)

        def hwrap(func,callback=True):
            return wrap(func, Order=2, callback=callback)

        if Algorithm == "powell":
            printcool("Minimizing Objective Function using\nPowell's Conjugate Direction Method" , ansi=1, bold=1)
            return optimize.fmin_powell(xwrap(self.Objective.Full),self.mvals0,ftol=self.convergence_objective,xtol=self.convergence_step,maxiter=self.maxstep)
        elif Algorithm == "simplex":
            printcool("Minimizing Objective Function using\nNelder-Mead Simplex Method" , ansi=1, bold=1)
            return optimize.fmin(xwrap(self.Objective.Full),self.mvals0,ftol=self.convergence_objective,xtol=self.convergence_step,maxiter=self.maxstep,maxfun=self.maxstep*10)
        elif Algorithm == "anneal":
            printcool("Minimizing Objective Function using Simulated Annealing" , ansi=1, bold=1)
            xmin, Jmin, T, feval, iters, accept, status = optimize.anneal(xwrap(self.Objective.Full), self.mvals0, lower=self.mvals0-1*self.trust0*np.ones(self.np),
                                                                          upper=self.mvals0+self.trust0*np.ones(self.np),schedule='boltzmann', full_output=True)
            scodes = {0 : "Points no longer changing.",
                      1 : "Cooled to final temperature.",
                      2 : "Maximum function evaluations reached.",
                      3 : "Maximum cooling iterations reached.",
                      4 : "Maximum accepted query locations reached.",
                      5 : "Final point not the minimum amongst encountered points."}
            logger.info("Simulated annealing info:\n")
            logger.info("Status: %s \n" % scodes[status])
            logger.info("Function evaluations: %i" % feval)
            logger.info("Cooling iterations:   %i" % iters)
            logger.info("Tests accepted:       %i" % iters)
            return xmin
        elif Algorithm == "basinhopping":
            printcool("Minimizing Objective Function using Basin Hopping Method" , ansi=1, bold=1)
            T = xwrap(self.Objective.Full)(self.mvals0)
            Result = optimize.basinhopping(xwrap(self.Objective.Full), self.mvals0, niter=self.maxstep, T=T, stepsize=self.trust0, interval=20,
                                           minimizer_kwargs={'method':'nelder-mead','options':{'xtol': self.convergence_step,'ftol':self.convergence_objective}}, disp=True)
            logger.info(Result.message + "\n")
            return Result.x
        elif Algorithm == "cg":
            printcool("Minimizing Objective Function using\nPolak-Ribiere Conjugate Gradient Method" , ansi=1, bold=1)
            return optimize.fmin_cg(xwrap(self.Objective.Full,callback=False),self.mvals0,fprime=gwrap(self.Objective.Full),gtol=self.convergence_gradient)
        elif Algorithm == "tnc":
            printcool("Minimizing Objective Function using\nTruncated Newton Algorithm (Unconfirmed)" , ansi=1, bold=1)
            Result = optimize.fmin_tnc(fgwrap(self.Objective.Full,callback=False),self.mvals0,
                                       maxfun=self.maxstep,ftol=self.convergence_objective,pgtol=self.convergence_gradient,xtol=self.convergence_objective)
            return Result.x
        elif Algorithm == "ncg":
            printcool("Minimizing Objective Function using\nNewton-CG Algorithm" , ansi=1, bold=1)
            Result = optimize.fmin_ncg(xwrap(self.Objective.Full,callback=False),self.mvals0,fprime=gwrap(self.Objective.Full,callback=False),
                                       fhess=hwrap(self.Objective.Full),avextol=self.convergence_objective,maxiter=self.maxstep,disp=True)
            return Result
        elif Algorithm == "bfgs":
            printcool("Minimizing Objective Function using\nBFGS Quasi-Newton Method" , ansi=1, bold=1)
            return optimize.fmin_bfgs(xwrap(self.Objective.Full,callback=False),self.mvals0,fprime=gwrap(self.Objective.Full),gtol=self.convergence_gradient)

    def GeneticAlgorithm(self):
        
        """ 
        Genetic algorithm, under development. It currently works but a
        genetic algorithm is more like a concept; i.e. there is no
        single way to implement it.
        
        @todo Massive parallelization hasn't been implemented yet

        """
        def generate_fresh(rows, cols):
            new_guys = np.zeros((rows, cols))
            for i in range(rows):
                new_guys[i, int(cols*np.random.random())] = self.trust0 * np.random.randn()
            return new_guys
        
        def cross_over(chrom1, chrom2):
            crosspt = 1 + int((len(chrom1) - 1) * np.random.random())
            Ans1 = np.hstack((chrom1[:crosspt],chrom2[crosspt:]))
            Ans2 = np.hstack((chrom2[:crosspt],chrom1[crosspt:]))
            return Ans1, Ans2

        def mutate(chrom):
            mutpt = int(len(chrom) * np.random.random())
            chrom[mutpt] += self.trust0 * np.random.randn()
            return chrom

        def initial_generation():
            return np.vstack((self.mvals0.copy(),np.random.randn(PopSize, self.FF.np)*self.trust0)) / (self.FF.np ** 0.5)
            #return np.vstack((self.mvals0.copy(),generate_fresh(PopSize, self.np)))

        def calculate_fitness(pop):
            return [self.Objective.Full(i,Order=0,verbose=False)['X'] for i in pop]

        def sort_by_fitness(fits):
            return np.sort(fits), np.argsort(fits)

        def generate_new_population(sorted, pop):
            newpop = pop[sorted[1]]
            # Individuals in this range are kept
            a = range(KeepNum)
            logger.info("Keeping: " + str(a) + '\n')
            random.shuffle(a)
            for i in range(0, KeepNum, 2):
                logger.info("%i and %i reproducing to replace %i and %i\n" % (a[i],a[i+1],len(newpop)-i-2,len(newpop)-i-1))
                newpop[-i-1], newpop[-i-2] = cross_over(newpop[a[i]],newpop[a[i+1]])
            b = range(KeepNum, len(newpop))
            random.shuffle(b)
            for i in b[:MutNum]:
                logger.info("Randomly mutating %i\n" % i)
                newpop[i] = mutate(newpop[i])
            return newpop
            
        def xwrap(func,verbose=True):
            def my_func(mvals):
                if verbose: logger.info('\n')
                Answer = func(mvals,Order=0,verbose=verbose)['X']
                dx = (my_func.x_best - Answer) if my_func.x_best != None else 0.0
                if Answer < my_func.x_best or my_func.x_best == None:
                    color = "\x1b[92m"
                    my_func.x_best = Answer
                else:
                    color = "\x1b[91m"
                if verbose:
                    logger.info("k=" + ' '.join(["% .4f" % i for i in mvals]) + '\n')
                    logger.info("X2= %s%12.3e\x1b[0m d(X2)= %12.3e\n" % (color,Answer,dx))
                return Answer
            my_func.x_best = None
            return my_func

        PopSize = 120
        KeepThresh = 0.5
        MutProb = 0.1
        CrosProb = 0.5
        
        KeepNum = int(KeepThresh * PopSize)
        MutNum = int(MutProb * PopSize)
        CrosNum = int(CrosProb/2 * PopSize) * 2
        Population = initial_generation()
        Gen = 0

        Best = [[],[]]

        while True:
            #print Population
            Fits = calculate_fitness(Population)
            Sorted = sort_by_fitness(Fits)
            logger.info(str(Sorted))
            Best[0].append(Sorted[0][0])
            Best[1].append(Population[Sorted[1][0]])
            logger.info(str(Best))
            if Gen == self.maxstep: break
            Population = generate_new_population(Sorted, Population)
            Gen += 1

        logger.info(str(Best))
        return Population[Sorted[1][0]]
        

    def Simplex(self):
        """ Use SciPy's built-in simplex algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="simplex")

    def Powell(self):
        """ Use SciPy's built-in Powell direction-set algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="powell")

    def Anneal(self):
        """ Use SciPy's built-in simulated annealing algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="anneal")

    def ConjugateGradient(self):
        """ Use SciPy's built-in conjugate gradient algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="cg")

    def Scipy_BFGS(self):
        """ Use SciPy's built-in BFGS algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="bfgs")

    def BasinHopping(self):
        """ Use SciPy's built-in basin hopping algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="basinhopping")

    def TruncatedNewton(self):
        """ Use SciPy's built-in truncated Newton (fmin_tnc) algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="tnc")

    def NewtonCG(self):
        """ Use SciPy's built-in Newton-CG (fmin_ncg) algorithm to optimize the parameters. @see Optimizer::ScipyOptimizer """
        return self.ScipyOptimizer(Algorithm="ncg")

    def Scan_Values(self,MathPhys=1):
        """ Scan through parameter values.

        This option is activated using the inputs:

        @code
        scan[mp]vals
        scan_vals low:hi:nsteps
        scan_idxnum (number) -or-
        scan_idxname (name)
        @endcode
        
        This method goes to the specified parameter indices and scans through
        the supplied values, evaluating the objective function at every step.

        I hope this method will be useful for people who just want to look at
        changing one or two parameters and seeing how it affects the force
        field performance.

        @todo Maybe a multidimensional grid can be done.
        @param[in] MathPhys Switch to use mathematical (True) or physical (False) parameters.
        
        """
        # First make sure that the user entered the correct syntax.
        try:
            vals_in = [float(i) for i in self.scan_vals.split(":")]
        except:
            logger.error("Syntax error: in the input file please use scan_vals low:hi:nsteps\n")
            raise RuntimeError
        if len(vals_in) != 3:
            logger.error("Syntax error: in the input file please use scan_vals low:hi:nsteps\n")
            raise RuntimeError
        idx = [int(i) for i in self.idxnum]
        for j in self.idxname:
            idx += [self.FF.map[i] for i in self.FF.map if j in i]
        idx = set(idx)
        scanvals = np.linspace(vals_in[0],vals_in[1],vals_in[2])
        logger.info(str(vals_in) + '\n')
        logger.info(str(scanvals) + '\n')
        for pidx in idx:
            if MathPhys:
                logger.info("Scanning parameter %i (%s) in the mathematical space\n" % (pidx,self.FF.plist[pidx]))
                vals = self.mvals0.copy()
            else:
                logger.info("Scanning parameter %i (%s) in the physical space\n" % (pidx,self.FF.plist[pidx]))
                self.FF.use_pvals = True
                vals = self.FF.pvals0.copy()
            for i in scanvals:
                vals[pidx] = i
                data        = self.Objective.Full(vals,Order=0)
                logger.info("Value = % .4e Objective = % .4e\n" % (i, data['X']))

    def ScanMVals(self):
        """ Scan through the mathematical parameter space. @see Optimizer::ScanValues """
        self.Scan_Values(1)

    def ScanPVals(self):
        """ Scan through the physical parameter space. @see Optimizer::ScanValues """
        self.Scan_Values(0)

    def SinglePoint(self):
        """ A single-point objective function computation. """
        data        = self.Objective.Full(self.mvals0,Order=0,verbose=True)
        printcool("Objective Function Single Point: %.8f" % data['X'])

    def Gradient(self):
        """ A single-point gradient computation. """
        data        = self.Objective.Full(self.mvals0,Order=1)
        bar = printcool("Objective function: %.8f\nGradient below" % data['X'])
        self.FF.print_map(vals=data['G'],precision=8)
        logger.info(bar)

    def Hessian(self):
        """ A single-point Hessian computation. """
        data        = self.Objective.Full(self.mvals0,Order=2)
        bar = printcool("Objective function: %.8f\nGradient below" % data['X'])
        self.FF.print_map(vals=data['G'],precision=8)
        logger.info(bar)
        printcool("Hessian matrix:")
        pmat2d(data['H'], precision=8)
        logger.info(bar)

    def FDCheckG(self):
        """ Finite-difference checker for the objective function gradient.

        For each element in the gradient, use a five-point finite difference
        stencil to compute a finite-difference derivative, and compare it to
        the analytic result.

        """

        Adata        = self.Objective.Full(self.mvals0,Order=1)['G']
        Fdata        = np.zeros(self.FF.np)
        printcool("Checking first derivatives by finite difference!\n%-8s%-20s%13s%13s%13s%13s" \
                  % ("Index", "Parameter ID","Analytic","Numerical","Difference","Fractional"),bold=1,color=5)
        for i in range(self.FF.np):
            Fdata[i] = f1d7p(fdwrap(self.Objective.Full,self.mvals0,i,'X',Order=0),self.h)
            Denom = max(abs(Adata[i]),abs(Fdata[i]))
            Denom = Denom > 1e-8 and Denom or 1e-8
            D = Adata[i] - Fdata[i]
            Q = (Adata[i] - Fdata[i])/Denom
            cD = abs(D) > 0.5 and "\x1b[1;91m" or (abs(D) > 1e-2 and "\x1b[91m" or (abs(D) > 1e-5 and "\x1b[93m" or "\x1b[92m"))
            cQ = abs(Q) > 0.5 and "\x1b[1;91m" or (abs(Q) > 1e-2 and "\x1b[91m" or (abs(Q) > 1e-5 and "\x1b[93m" or "\x1b[92m"))
            logger.info("\r    %-8i%-20s% 13.4e% 13.4e%s% 13.4e%s% 13.4e\x1b[0m\n" \
                  % (i, self.FF.plist[i][:20], Adata[i], Fdata[i], cD, D, cQ, Q))

    def FDCheckH(self):
        """ Finite-difference checker for the objective function Hessian.

        For each element in the Hessian, use a five-point stencil in both
        parameter indices to compute a finite-difference derivative, and
        compare it to the analytic result.

        This is meant to be a foolproof checker, so it is pretty slow.  We
        could write a faster checker if we assumed we had accurate first
        derivatives, but it's better to not make that assumption.

        The second derivative is computed by double-wrapping the objective
        function via the 'wrap2' function.

        """
        Adata        = self.Objective.Full(self.mvals0,Order=2)['H']
        Fdata        = np.zeros((self.FF.np,self.FF.np))
        printcool("Checking second derivatives by finite difference!\n%-8s%-20s%-20s%13s%13s%13s%13s" \
                  % ("Index", "Parameter1 ID", "Parameter2 ID", "Analytic","Numerical","Difference","Fractional"),bold=1,color=5)

        # Whee, our double-wrapped finite difference second derivative!
        def wrap2(mvals0,pidxi,pidxj):
            def func1(arg):
                mvals = list(mvals0)
                mvals[pidxj] += arg
                return f1d5p(fdwrap(self.Objective.Full,mvals,pidxi,'X',Order=0),self.h)
            return func1
        
        for i in range(self.FF.np):
            for j in range(i,self.FF.np):
                Fdata[i,j] = f1d5p(wrap2(self.mvals0,i,j),self.h)
                Denom = max(abs(Adata[i,j]),abs(Fdata[i,j]))
                Denom = Denom > 1e-8 and Denom or 1e-8
                D = Adata[i,j] - Fdata[i,j]
                Q = (Adata[i,j] - Fdata[i,j])/Denom
                cD = abs(D) > 0.5 and "\x1b[1;91m" or (abs(D) > 1e-2 and "\x1b[91m" or (abs(D) > 1e-5 and "\x1b[93m" or "\x1b[92m"))
                cQ = abs(Q) > 0.5 and "\x1b[1;91m" or (abs(Q) > 1e-2 and "\x1b[91m" or (abs(Q) > 1e-5 and "\x1b[93m" or "\x1b[92m"))
                logger.info("\r    %-8i%-20s%-20s% 13.4e% 13.4e%s% 13.4e%s% 13.4e\x1b[0m\n" \
                      % (i, self.FF.plist[i][:20], self.FF.plist[j][:20], Adata[i,j], Fdata[i,j], cD, D, cQ, Q))

    def readchk(self):
        """ Read the checkpoint file for the main optimizer. """
        self.chk = {}
        if self.rchk_fnm != None:
            absfnm = os.path.join(self.root,self.rchk_fnm)
            if os.path.exists(absfnm):
                self.chk = pickle.load(open(absfnm))
            else:
                logger.info("\x1b[40m\x1b[1;92mWARNING:\x1b[0m read_chk is set to True, but checkpoint file not loaded (wrong filename or doesn't exist?)\n")
        return self.chk

    def writechk(self):
        """ Write the checkpoint file for the main optimizer. """
        if self.wchk_fnm != None:
            logger.info("Writing the checkpoint file %s\n" % self.wchk_fnm)
            with wopen(os.path.join(self.root,self.wchk_fnm)) as f: pickle.dump(self.chk,f)
        
