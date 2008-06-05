import sys
import os
import string
import optparse
#import traceback # for debugging. use: traceback.print_stack()

import error
import debug
import plugin
import parsers.yang
import parsers.yin
import parsers.grammar
import util

pyang_version = '0.9.0b'

## FIXME: move Repos, Context etc to maybe lib.py
class Repository(object):
    """Abstract base class that represents a module repository"""

    def __init__(self):
        pass

    def get_module(self, modulename):
        """Return the raw module text from the repository

        Returns (`ref`, `format`, `text`) if found, or None if not found.
        `ref` is a string which is used to identify the source of
              the text for the user.  used in error messages
        `format` is one of 'yang' or 'yin'.
        `text` is the raw text data

        Raises `ReadError`
        """

    class ReadError(Exception):
        """Signals that an error occured during module retrieval"""

        def __init__(self, str):
            Exception.__init__(self, str)

class FileRepository(Repository):
    def __init__(self, path):
        Repository.__init__(self)
        self.path = path

    def _search_file(self, filename):
        """Given a search path, find file"""

        paths = string.split(self.path, os.pathsep)
        for path in paths:
            fname = os.path.join(path, filename)
            if fname.startswith("./"):
                fname = fname[2:]
            if os.path.exists(fname):
                return fname
        return None

    def get_module(self, modulename):
        filename = self._search_file(modulename + ".yang")
        format = 'yang'
        if filename == None:
            filename = self._search_file(modulename + ".yin")
            format = 'yin'
        if filename == None:
            filename = self._search_file(modulename)
            format = None
        if filename == None:
            return None
        try:
            fd = file(filename)
            text = fd.read()
        except IOError, ex:
            raise self.ReadError(filename + ": " + str(ex))

        if format is None:
            format = guess_format(text)
        return (filename, format, text)

class Context(object):
    """Class which encapsulates a parse session"""

    def __init__(self, repository):
        """`repository` is a `Repository` instance"""
        
        self.modules = {}
        """dict of modulename:<class Module>)"""
        
        self.module_list = []
        """ordered list of modules; we must validate in this order"""
        
        self.repository = repository
        self.errors = []
        self.canonical = False
        self.submodule_expansion = True

    def add_module(self, ref, format, text):
        """Parse a module text and add the module data to the context

        `ref` is a string which is used to identify the source of
              the text for the user.  used in error messages
        `format` is one of 'yang' or 'yin'.
        `text` is the raw text data

        Returns the parsed module on success, and None on error.
        """

        if format == 'yin':
            p = parsers.yin.YinParser()
        else:
            p = parsers.yang.YangParser()

        module = p.parse(self, ref, text)
        if module == None:
            return None

        parsers.grammar.chk_module_statements(self, module, self.canonical)
        self._set_attrs(module)

        if module.name not in self.modules or self.modules[module.name] == None:
            self.modules[module.name] = module
            self.module_list.append(module)
        return module

    def del_module(self, module):
        """Remove a module from the context"""

        del self.modules[module.name]
        self.module_list.remove(module)

    def search_module(self, pos, modulename):
        """Searches for a module named `modulename` in the repository

        If the module is found, it is added to the context.
        Returns the module if found, and None otherwise"""
        if modulename in self.modules:
            return self.modules[modulename]
        try:
            r = self.repository.get_module(modulename)
            if r == None:
                error.err_add(self.errors, pos, 'MODULE_NOT_FOUND', modulename)
                return None
        except self.repository.ReadError, ex:
            error.err_add(self.errors, pos, 'READ_ERROR', str(ex))
        (ref, format, text) = r
        module = self.add_module(ref, format, text)
        if modulename != module.name:
            error.err_add(self.errors, module.pos, 'BAD_MODULE_FILENAME',
                          (module.name, filename, modulename))
            self.del_module(module)
            return None
        return module

    def _set_attrs(self, stmt):
        """temporary function which sets class attributes for substatements"""

        def get_occurance(subkeywd):
            def find(spec):
                for (keywd, occ) in spec:
                    if keywd == subkeywd:
                        return occ
                    if keywd == '$choice':
                        for s in occ:
                            r = find(s)
                            if r is not None:
                                return r
                    if keywd == '$interleave':
                        r = find(occ)
                        if r is not None:
                            return r
                return None
            if util.is_prefixed(stmt.keyword): return '*'
            (_arg_type, children) = parsers.grammar.stmt_map[stmt.keyword]
            return find(children)

        def get_attr(keywd):
            if keywd == 'import': return "import_"
            if keywd in ['leaf', 'container', 'leaf-list', 'list', 'anyxml',
                         'case', 'choice', 'uses', 'rpc', 'notification']:
                return 'children'
            if util.is_prefixed(keywd): return None
            return keywd.replace('-','_')

        for s in stmt.substmts:
            occurance = get_occurance(s.keyword)
            attr = get_attr(s.keyword)
            if attr is not None:
                if occurance == '?' or occurance == '1':
                    # single-instance attribute
                    stmt.__dict__[attr] = s
                else:
                    # make sure the substmt is not already defined
                    if (s.keyword != 'augment' and
                        s.keyword != 'type' and
                        util.attrsearch(s.arg, 'arg', stmt.__dict__[attr])):
                        error.err_add(self.errors, s.pos,
                                      'DUPLICATE_STATEMENT', s.arg)
                    stmt.__dict__[attr].append(s)
            self._set_attrs(s)
            if s.keyword == 'import':
                s.parent.set_import(s)
            elif s.keyword == 'include':
                s.parent.set_include(s)

    def validate(self):
        uris = {}
        for modname in self.modules:
            m = self.modules[modname]
            if m != None and m.namespace != None:
                uri = m.namespace.arg
                if uri in uris:
                    error.err_add(self.errors, m.namespace.pos,
                                  'DUPLICATE_NAMESPACE', (uri, uris[uri]))
                else:
                    uris[uri] = m.name
        for m in self.module_list:
            if m != None:
                m.validate()


### YANG grammar

    # keyword             argument-name  yin-element xsd-appinfo
yang_keywords = \
    {'anyxml':           ('name',        False,      False),
     'argument':         ('name',        False,      False),
     'augment':          ('target-node', False,      False),
     'belongs-to':       ('module',      False,      True),
     'bit':              ('name',        False,      False),
     'case':             ('name',        False,      False),
     'choice':           ('name',        False,      False),
     'config':           ('value',       False,      True),
     'contact':          ('info',        True,       True),
     'container':        ('name',        False,      False),
     'default':          ('value',       False,      True),
     'description':      ('text',        True,       False),
     'enum':             ('name',        False,      False),
     'error-app-tag':    ('value',       False,      True),
     'error-message':    ('value',       True,       True),
     'extension':        ('name',        False,      False),
     'grouping':         ('name',        False,      False),
     'import':           ('module',      False,      True),
     'include':          ('module',      False,      True),
     'input':            (None,          None,       False),
     'key':              ('value',       False,      False),
     'leaf':             ('name',        False,      False),
     'leaf-list':        ('name',        False,      False),
     'length':           ('value',       False,      False),
     'list':             ('name',        False,      False),
     'mandatory':        ('value',       False,      True),
     'max-elements':     ('value',       False,      True),
     'min-elements':     ('value',       False,      True),
     'module':           ('name',        False,      False),
     'must':             ('condition',   False,      True),
     'namespace':        ('uri',         False,      False),
     'notification':     ('name',        False,      False),
     'ordered-by':       ('value',       False,      True),
     'organization':     ('info',        True,       True),
     'output':           (None,          None,       False),
     'path':             ('value',       False,      False),
     'pattern':          ('value',       False,      False),
     'position':         ('value',       False,      False),
     'presence':         ('value',       False,      False),
     'prefix':           ('value',       False,      True),
     'range':            ('value',       False,      False),
     'reference':        ('info',        False,      True),
     'revision':         ('date',        False,      True),
     'rpc':              ('name',        False,      False),
     'status':           ('value',       False,      True),
     'submodule':        ('name',        False,      False),
     'type':             ('name',        False,      False),
     'typedef':          ('name',        False,      False),
     'unique':           ('tag',         False,      False),
     'units':            ('name',        False,      True),
     'uses':             ('name',        False,      False),
     'value':            ('value',       False,      False),
     'when':             ('condition',   False,      True),
     'yang-version':     ('value',       False,      True),
     'yin-element':      ('value',       False,      False),
     }


def guess_format(text):
    """Guess YANG/YIN format
    
    If the first non-whitespace character is '<' then it is XML.
    Return 'yang' or 'yin'"""
    format = 'yang'
    i = 0
    while i < len(text) and text[i].isspace():
        i += 1
    if i < len(text):
        if text[i] == '<':
            format = 'yin'
    return format

### main pyang program

def run():
    usage = """%prog [options] <filename>...

Validates the YANG module in <filename>, and all its dependencies."""

    plugindirs = []
    # check for --plugindir
    idx = 1
    while '--plugindir' in sys.argv[idx:]:
        idx = idx + sys.argv[idx:].index('--plugindir')
        plugindirs.append(sys.argv[idx+1])
        idx = idx + 1
    plugin.init(plugindirs)

    fmts = {}
    for p in plugin.plugins:
        p.add_output_format(fmts)

    optlist = [
        # use capitalized versions of std options help and version
        optparse.make_option("-h", "--help",
                             action="help",
                             help="Show this help message and exit"),
        optparse.make_option("-v", "--version",
                             action="version",
                             help="Show version number and exit"),
        optparse.make_option("-e", "--list-errors",
                             dest="list_errors",
                             action="store_true",
                             help="Print a listing of all error codes " \
                             "and exit."),
        optparse.make_option("--print-error-code",
                             dest="print_error_code",
                             action="store_true",
                             help="On errors, print the error code instead " \
                             "of the error message."),
        optparse.make_option("-l", "--level",
                             dest="level",
                             default=3,
                             type="int",
                             help="Report errors and warnings up to LEVEL. " \
                             "If any error or warnings are printed, the " \
                             "program exits with exit code 1, otherwise " \
                             "with exit code 0. The default error level " \
                             "is 3."),
        optparse.make_option("--canonical",
                             dest="canonical",
                             action="store_true",
                             help="Validate the module(s) according the " \
                             "canonical YANG order."),
        optparse.make_option("-f", "--format",
                             dest="format",
                             help="Convert to FORMAT.  Supported formats " \
                             "are " +  ', '.join(fmts.keys())),
        optparse.make_option("-o", "--output",
                             dest="outfile",
                             help="Write the output to OUTFILE instead " \
                             "of stdout."),
        optparse.make_option("-p", "--path", dest="path", default="",
                             help="Search path for yin and yang modules"),
        optparse.make_option("--plugindir",
                             dest="plugindir",
                             help="Loads pyang plugins from PLUGINDIR"),
        optparse.make_option("-d", "--debug",
                             dest="debug",
                             action="store_true",
                             help="Turn on debugging of the pyang code"),
        ]
        
    optparser = optparse.OptionParser(usage, add_help_option = False)
    optparser.version = '%prog ' + pyang_version
    optparser.add_options(optlist)

    for p in plugin.plugins:
        p.add_opts(optparser)

    (o, args) = optparser.parse_args()

    if o.list_errors == True:
        for tag in error.error_codes:
            (level, fmt) = error.error_codes[tag]
            print "Error:   %s - level %d" % (tag, level)
            print "Message: %s" % fmt
            print ""
        sys.exit(0)

    if o.outfile != None and o.format == None:
        print >> sys.stderr, "no format specified"
        sys.exit(1)
    if o.format != None and len(args) > 1:
        print >> sys.stderr, "too many files to convert"
        sys.exit(1)
    if len(args) == 0:
        print >> sys.stderr, "no file given"
        sys.exit(1)

    filenames = args

    debug.set_debug(o.debug)

    basedir = os.path.split(sys.modules['pyang'].__file__)[0]
    path = o.path + ':' + basedir + '/../modules' + ':.'
    repos = FileRepository(path)

    ctx = Context(repos)
    ctx.canonical = o.canonical
    ctx.opts = o
    # temporary hack. needed for yin plugin
    ctx.filename = args[0]
    
    if o.format != None:
        emit_obj = fmts[o.format]
    else:
        emit_obj = None

    if emit_obj != None:
        emit_obj.setup_ctx(ctx)

    for filename in filenames:
        try:
            fd = file(filename)
            text = fd.read()
        except IOError, ex:
            sys.stderr.write("error %s: %s\n" % (filename, str(ex)))
            sys.exit(1)
        format = guess_format(text)
        module = ctx.add_module(filename, format, text)

    ctx.validate()
    exit_code = 0
    for (epos, etag, eargs) in ctx.errors:
        elevel = error.err_level(etag)
        if elevel <= o.level:
            if o.print_error_code == True:
                print >> sys.stderr, \
                      str(epos) + ': [%d] %s' % (elevel, etag)
            else:
                print >> sys.stderr, \
                      str(epos) + ': [%d] ' % \
                      elevel + error.err_to_str(etag, eargs)
            exit_code = 1
    if o.outfile == None:
        writef = lambda str: sys.stdout.write(str)
    else:
        fd = open(o.outfile, "w+")
        writef = lambda str: fd.write(str)
    if emit_obj != None:
        emit_obj.emit(ctx, module, writef)

    sys.exit(exit_code)
