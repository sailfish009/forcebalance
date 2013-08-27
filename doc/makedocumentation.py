"""This file contains a documentation generating script. Doxygen
is used to do the actual generation, so these functions act primarily to
streamline the process and provide some customizations to the automatically
generated documents
"""

import os, sys
import re
import shutil
import subprocess
import argparse
from traceback import print_exc
from socket import gethostname
from datetime import datetime

def build(interactive=False, upstream=False):

    if interactive:
        display = lambda txt : raw_input("$ %s " % txt)
    else:
        display = lambda txt : sys.stdout.write("$ %s\n" % txt)

    print "\n# Build list of documented options"
    display("python make-option-index.py > option_index.txt")
    os.system("python make-option-index.py > option_index.txt")

    # generate pages to be included in general documentation
    mainpage=""
    mainpage+="/**\n\n\\mainpage\n\n"
    for fnm in ["introduction.txt", "installation.txt", "usage.txt", "tutorial.txt", "glossary.txt", "option_index.txt"]:
        page=open(fnm,'r')
        mainpage+=page.read()
    mainpage+="\n\\image latex ForceBalance.pdf \"Logo.\" height=10cm\n\n*/"

    # generate pages to be included in API documentation
    api=""
    api+="/**\n\n"
    for fnm in ["roadmap.txt"]:
        page=open(fnm,'r')
        api+=page.read()
    api+="\n\n*/"
    
    print "\n# Switch to documentation branch before writing files"
    display("git checkout gh-pages")
    if os.system("git checkout gh-pages"):
        sys.exit(1)
    
    # try to write changes to new branch. If anything goes wrong, reset and return to master
    try:
        with open('mainpage.dox','w') as f:
            f.write(mainpage)
        with open('api.dox','w') as f:
            f.write(api)
        
        # Run doxygen to generate general documentation
        print "\n# run doxygen with doxygen.cfg as input to generate general documentation"
        display("doxygen doxygen.cfg")
        if subprocess.call(['doxygen', 'doxygen.cfg']): raise OSError("Doxygen returned nonzero value while working on doxygen.cfg")

        # Run doxygen to generate technical (API) documentation
        print "\n# run doxygen with api.cfg as input to generate API documentation"
        display("doxygen api.cfg")
        if subprocess.call(['doxygen', 'api.cfg']): raise OSError("Doxygen returned nonzero value while working on api.cfg")
        # add_tabs script adjusts html
        print "\n# run add_tabs function to adjust tabs on html generated by doxygen"
        display("python -c 'from makedocumentation import add_tabs; add_tabs()'")
        add_tabs(fnm)
        
        # Compile pdf formats
        print "\n# Copy images referenced in latex files to proper folders"
        display("cp Images/ForceBalance.pdf latex/ && cp Images/ForceBalance.pdf latex/api/")
        if not os.path.exists('latex/api'):
            os.makedirs('latex/api')
        shutil.copy('Images/ForceBalance.pdf','latex/')
        shutil.copy('Images/ForceBalance.pdf','latex/api/')
        
        print "\n# Compile generated latex documentation into pdf"
        display("cd latex && make")
        os.chdir('latex')
        if subprocess.call(['make']): raise OSError("make returned nonzero value while compiling latex/")
        print "\n# Copy generated pdf up to /doc directory"
        display("cd .. && cp latex/refman.pdf ForceBalance-Manual.pdf")
        os.chdir('..')
        shutil.copy('latex/refman.pdf', 'ForceBalance-Manual.pdf')
        
        print "\n#Compile generated latex API documentation into pdf"
        display("cd latex/api/ && make")
        os.chdir('latex/api/')
        if subprocess.call(['make']): raise OSError("make returned nonzero value while compiling latex/api/")
        print "\n# Copy generated API pdf up to /doc directory"
        display("cd ../.. && cp latex/api/refman.pdf ForceBalance-API.pdf")
        os.chdir('../..')
        shutil.copy('latex/api/refman.pdf', 'ForceBalance-API.pdf')
        
        print "\n# Stage changes for commit"
        display("git add .")
        if os.system('git add .'): raise OSError("Error trying to stage files for commit")
        print"\n# Commit changes locally"
        display('git commit -m "Automatic documentation generation at %s on %s"' % (gethostname(), datetime.now().strftime("%m-%d-%Y %H:%M")))
        if os.system('git commit -m "Automatic documentation generation at %s on %s"' % (gethostname(), datetime.now().strftime("%m-%d-%Y %H:%M"))):
            raise OSError("Error trying to commit files to local gh-pages branch")
        
        # push changes upstream if upstream option was given
        if upstream:
            try:
                print "\n# Push updated documentation upstream"
                display("git push")
                if os.system('git push'): raise OSError("While trying to push changes upstream 'git push' gave a nonzero return code")
            except:
                print_exc()
                upstream = False  # changes could not be pushed upstream so we should switch to the local mode
                raw_input("\n# encountered ERROR. Documentation could not be pushed upstream.")
        
    except:
        print_exc()
        upstream = False  # since documentation generation failed,
        raw_input("\n# encountered ERROR (above). Documentation could not be generated.") 
        
        print "\n# Putting any uncommmited changes on the stash"
        display("git stash")
        os.system('git stash')
        
    else:
        print "Documentation successfully generated"
    finally:
        print "\n# Switch back to master branch"
        display("git checkout master")
        os.system('git checkout master')
        
        if upstream:
            print "\n# Remove local copy of successfully pushed documentation branch"
            display("git branch -D gh-pages")
            os.system('git branch -D gh-pages')
    

def add_tabs(fnm):
    """Adjust tabs in html version of documentation"""
    for fnm in os.listdir('./html/'):
        if re.match('.*\.html$',fnm):
            fnm = './html/' + fnm
            newfile = []
            installtag = ' class="current"' if fnm.split('/')[-1] == 'installation.html' else ''
            usagetag = ' class="current"' if fnm.split('/')[-1] == 'usage.html' else ''
            tutorialtag = ' class="current"' if fnm.split('/')[-1] == 'tutorial.html' else ''
            glossarytag = ' class="current"' if fnm.split('/')[-1] == 'glossary.html' else ''
            todotag = ' class="current"' if fnm.split('/')[-1] == 'todo.html' else ''
            
            for line in open(fnm):
                newfile.append(line)
                if re.match('.*a href="index\.html"',line):
                    newfile.append('      <li%s><a href="installation.html"><span>Installation</span></a></li>\n' % installtag)
                    newfile.append('      <li%s><a href="usage.html"><span>Usage</span></a></li>\n' % usagetag)
                    newfile.append('      <li%s><a href="tutorial.html"><span>Tutorial</span></a></li>\n' % tutorialtag)
                    newfile.append('      <li%s><a href="glossary.html"><span>Glossary</span></a></li>\n' % glossarytag)
                    newfile.append('      <li><a href="api/roadmap.html"><span>API</span></a></li>\n')
            with open(fnm,'w') as f: f.writelines(newfile)

    for fnm in os.listdir('./html/api/'):
        if re.match('.*\.html$',fnm):
            fnm = './html/api/' + fnm
            newfile=[]
            for line in open(fnm):
                if re.match('.*a href="index\.html"',line):
                    newfile.append('      <li><a href="../index.html"><span>Main Page</span></a></li>\n')
                    newfile.append('      <li ')
                    if re.match('.*roadmap\.html$', fnm): newfile.append('class="current"')
                    newfile.append('><a href="roadmap.html"><span>Project Roadmap</span></a></li>\n')
                else: newfile.append(line)
            with open(fnm,'w') as f: f.writelines(newfile)

def find_forcebalance():
    """try to find forcebalance location in standard python path"""
    forcebalance_dir=""
    try:
        import forcebalance
        forcebalance_dir = forcebalance.__path__[0]
    except:
        print "Unable to find forcebalance directory in PYTHON PATH (Is it installed?)"
        print "Try running forcebalance/setup.py or you can always set the INPUT directory"
        print "manually in api.cfg"
        exit()

    print 'ForceBalance directory is:', forcebalance_dir
    return forcebalance_dir

def find_doxypy():
    """Check if doxypy is in system path or else ask for location of doxypy.py"""
    doxypy_path=""
    try:
        # first check to see if doxypy is in system path
        if subprocess.call(["doxypy", "makedocumentation.py"],stdout=open(os.devnull)): raise OSError()
        doxypy_path="doxypy"
    except OSError: 
        doxypy_path=raw_input("Enter location of doxypy.py: ")
        if not os.path.exists(doxypy_path) or doxypy_path[-9:] != 'doxypy.py':
            print "Invalid path to doxypy"
            exit()
    return doxypy_path
    
def build_config():
    forcebalance_path = find_forcebalance()
    doxypy_path = find_doxypy()

    with open('.doxygen.cfg', 'r') as f:
        lines = f.readlines()
        
    with open('doxygen.cfg', 'w') as f:
        for line in lines:
            if line.startswith('FILTER_PATTERNS        ='):
                option = 'FILTER_PATTERNS        = "*.py=' + doxypy_path + '"\n'
                f.write(option)
            else:
                f.write(line)
    
    with open('.api.cfg', 'r') as f:
        lines = f.readlines()
        
    with open('api.cfg', 'w') as f:
        for line in lines:
            if line.startswith('INPUT                  ='):
                option = 'INPUT                  = api.dox ' + forcebalance_path + '\n'
                f.write(option)
            elif line.startswith('FILTER_PATTERNS        ='):
                option = 'FILTER_PATTERNS        = "*.py=' + doxypy_path + '"\n'
                f.write(option)
            else:
                f.write(line)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--interactive', '-i', action='store_true', help="run in interactive mode, pausing before each command")
    parser.add_argument('--clean', '-k', action='store_true', help="remove temporary files after script is complete")
    parser.add_argument('--configure', '-c', action='store_true', help="generate doxygen configuration files from templates")
    parser.add_argument('--upstream', '-u', action='store_true', help="push updated documentation to upstream github repository")
    args = parser.parse_args()
    
    if args.configure:
        build_config()
    elif not os.path.isfile('doxygen.cfg') or not os.path.isfile('api.cfg'):
        print "Couldn't find required doxygen config files ('./doxygen.cfg' and './api.cfg').\nRun with --configure option to generate automatically"
        sys.exit(1)
    
    build(interactive = args.interactive, upstream = args.upstream)
    
    if args.clean:
        print "Cleaning up..."
        os.system("rm -rf latex option_index.txt api.dox mainpage.dox")   # cleanup
    
    print "#===================================#"
    print "#| Make sure to update the version |#"
    print "#| manually in doc/header.tex and  |#"
    print "#| doc/api_header.tex!!            |#"
    print "#===================================#"
        
