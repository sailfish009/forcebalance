"""@package BaseReader Base class for force field line reader.

@author Lee-Ping Wang
@date 12/2011
"""

from re import split, findall

class BaseReader(object):
    """ The 'reader' class.  It serves two main functions:

    1) When parsing a text force field file, the 'feed' method is
    called once for every line.  Calling the 'feed' method stores the
    internal variables that are needed for making the unique parameter
    identifier.

    2) The 'reader' also stores the 'pdict' dictionary, which is
    needed for building the matrix of rescaling factors.  This is not
    needed for the XML force fields, so in XML force fields pdict is
    replaced with a string called "XML_Override".

    """

    def __init__(self,fnm):
        self.ln     = 0
        self.itype  = fnm
        self.suffix = ''
        self.pdict  = {}

    def Split(self, line):
        return line.split()
    
    def Whites(self, line):
        return findall('[ ]+',line)
    
    def feed(self,line):
        self.ln += 1
        
    def build_pid(self, pfld):
        """ Returns the parameter type (e.g. K in BONDSK) based on the
        current interaction type.

        Both the 'pdict' dictionary (see gmxio.pdict) and the
        interaction type 'state' (here, BONDS) are needed to get the
        parameter type.

        If, however, 'pdict' does not contain the ptype value, a suitable
        substitute is simply the field number.

        Note that if the interaction type state is not set, then it
        defaults to the file name, so a generic parameter ID is
        'filename.line_num.field_num'
        
        """
        #print self.pdict[self.itype][pfld]
        ptype = self.pdict.get(self.itype,{}).get(pfld,':%i.%i' % (self.ln,pfld))
        return self.itype+ptype+self.suffix

