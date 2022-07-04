#!/usr/bin/env python3
#
# Copyright (c) 2010-2016 Varnish Software
# All rights reserved.
#
# Author: Poul-Henning Kamp <phk@phk.freebsd.dk>
#
# SPDX-License-Identifier: BSD-2-Clause
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

"""
Read the first existing file from arguments or vmod.vcc and produce:
    ${prefix}.h -- Prototypes for the implementation
    ${prefix}.c -- Magic glue & datastructures to make things a VMOD.
    vmod_${name}.rst -- Extracted documentation
    vmod_${name}.man.rst -- Extracted documentation (rst2man input)

    prefix can set via -o and defaults to vcc_if
"""

import copy
import glob
import hashlib
import json
import optparse
import os
import re
import sys
import time

AMBOILERPLATE = '''\
# Generated by vmodtool.py --boilerplate.

vmod_LTLIBRARIES += libvmod_XXX.la

libvmod_XXX_la_SOURCES = \\
\tSRC

libvmod_XXX_la_CFLAGS =

vmodtoolargs_XXX ?= --strict --boilerplate -o PFX
vmod_XXX_symbols_regex ?= Vmod_XXX_Data

libvmod_XXX_la_LDFLAGS = \\
\t-export-symbols-regex $(vmod_XXX_symbols_regex) \\
\t$(AM_LDFLAGS) \\
\t$(VMOD_LDFLAGS)

nodist_libvmod_XXX_la_SOURCES = PFX.c PFX.h

EXTRA_libvmod_XXX_la_DEPENDENCIES = $(nodist_libvmod_XXX_la_SOURCES)

EXTRA_DIST += $(srcdir)/VCC automake_boilerplate_XXX.am

$(libvmod_XXX_la_OBJECTS): PFX.h

PFX.h vmod_XXX.rst vmod_XXX.man.rst: PFX.c

PFX.c: $(vmodtool) $(srcdir)/VCC
\t@PYTHON@ $(vmodtool) $(vmodtoolargs_XXX) $(srcdir)/VCC

clean-local: clean-vmod-XXX

clean-vmod-XXX:
	rm -f $(nodist_libvmod_XXX_la_SOURCES)
	rm -f vmod_XXX.rst vmod_XXX.man.rst
'''

PRIVS = {
    'PRIV_CALL':   "struct vmod_priv *",
    'PRIV_VCL':    "struct vmod_priv *",
    'PRIV_TASK':   "struct vmod_priv *",
    'PRIV_TOP':    "struct vmod_priv *",
}

CTYPES = {
    'ACL':         "VCL_ACL",
    'BACKEND':     "VCL_BACKEND",
    'BLOB':        "VCL_BLOB",
    'BODY':        "VCL_BODY",
    'BOOL':        "VCL_BOOL",
    'BYTES':       "VCL_BYTES",
    'DURATION':    "VCL_DURATION",
    'ENUM':        "VCL_ENUM",
    'HEADER':      "VCL_HEADER",
    'HTTP':        "VCL_HTTP",
    'INT':         "VCL_INT",
    'IP':          "VCL_IP",
    'PROBE':       "VCL_PROBE",
    'REAL':        "VCL_REAL",
    'REGEX':       "VCL_REGEX",
    'STEVEDORE':   "VCL_STEVEDORE",
    'STRANDS':     "VCL_STRANDS",
    'STRING':      "VCL_STRING",
    'SUB':         "VCL_SUB",
    'TIME':        "VCL_TIME",
    'VOID':        "VCL_VOID",
}

CTYPES.update(PRIVS)

DEPRECATED = {}

#######################################################################

def deprecated(key, txt):
    '''
       Be annoying about features which are going away
    '''
    if DEPRECATED.get(key):
        return
    sys.stderr.write('#' * 72 + '\n')
    sys.stderr.write(txt + '\n')
    sys.stderr.write('#' * 72 + '\n')
    time.sleep(3)
    DEPRECATED[key] = True

#######################################################################

def is_quoted(txt):
    return len(txt) > 2 and txt[0] == txt[-1] and txt[0] in ('"', "'")

def unquote(txt):
    assert is_quoted(txt)
    return txt[1:-1]

def fmt_cstruct(fo, a, b):
    ''' Output line in vmod struct '''
    t = '\t%s' % a
    if len(t.expandtabs()) > 40:
        t += '\n\t\t\t\t\t'
    else:
        t += '\t'
    while len(t.expandtabs()) < 40:
        t += '\t'
    fo.write('%s%s\n' % (t, b))

#######################################################################


def write_file_warning(fo, a, b, c, s):
    fo.write(a + "\n")
    fo.write(b + " NB:  This file is machine generated, DO NOT EDIT!\n")
    fo.write(b + "\n")
    fo.write(b + " Edit " + s + " and run make instead\n")
    fo.write(c + "\n\n")


def write_c_file_warning(fo, s):
    write_file_warning(fo, "/*", " *", " */", s)


def write_rst_file_warning(fo, s):
    write_file_warning(fo, "..", "..", "..", s)


def write_rst_hdr(fo, s, below="-", above=None):
    fo.write('\n')
    if above:
        fo.write(above * len(s) + "\n")
    fo.write(s + "\n")
    if below:
        fo.write(below * len(s) + "\n")

#######################################################################


def lwrap(s, width=64):
    """
    Wrap a C-prototype like string into a number of lines.
    """
    ll = []
    p = ""
    while len(s) > width:
        y = s[:width].rfind(',')
        if y == -1:
            y = s[:width].rfind('(')
        if y == -1:
            break
        ll.append(p + s[:y + 1])
        s = s[y + 1:].lstrip()
        p = "    "
    if s:
        ll.append(p + s)
    return "\n".join(ll) + "\n"


#######################################################################


inputline = None


def err(txt, warn=True):
    if inputline is not None:
        print("While parsing line:\n\t", inputline)
    if opts.strict or not warn:
        print("ERROR: " + txt, file=sys.stderr)
        exit(1)
    else:
        print("WARNING: " + txt, file=sys.stderr)

#######################################################################


class CType():
    def __init__(self, wl, enums):
        self.nm = None
        self.defval = None
        self.spec = None
        self.opt = False

        self.vt = wl.pop(0)
        self.ct = CTYPES.get(self.vt)
        if self.ct is None:
            err("Expected type got '%s'" % self.vt, warn=False)
        if wl and wl[0] == "{":
            if self.vt != "ENUM":
                err("Only ENUMs take {...} specs", warn=False)
            self.add_spec(wl, enums)

    def __str__(self):
        s = "<" + self.vt
        if self.nm is not None:
            s += " " + self.nm
        if self.defval is not None:
            s += " VAL=" + self.defval
        if self.spec is not None:
            s += " SPEC=" + str(self.spec)
        return s + ">"

    def add_spec(self, wl, enums):
        assert self.vt == "ENUM"
        assert wl.pop(0) == "{"
        self.spec = []
        while True:
            x = wl.pop(0)
            if is_quoted(x):
                x = unquote(x)
            assert x
            self.spec.append(x)
            enums[x] = True
            w = wl.pop(0)
            if w == "}":
                break
            assert w == ","

    def vcl(self, terse=False):
        if self.vt == "STRANDS":
            return "STRING"
        if terse:
            return self.vt
        if self.spec is None:
            return self.vt
        return self.vt + " {" + ", ".join(self.spec) + "}"

    def jsonproto(self, jl):
        jl.append([self.vt])
        while jl[-1][-1] is None:
            jl[-1].pop(-1)

#######################################################################


class arg(CType):

    ''' Parse front of word list into argument '''

    def __init__(self, wl, argnames, enums, end):
        super(arg, self).__init__(wl, enums)

        if wl[0] == end:
            return

        x = wl.pop(0)
        if x in argnames:
            err("Duplicate argument name '%s'" % x, warn=False)
        argnames[x] = True
        self.nm = x

        if wl[0] == end:
            return

        x = wl.pop(0)
        if x != "=":
            err("Expected '=' got '%s'" % x, warn=False)

        x = wl.pop(0)
        if self.vt == "ENUM":
            if is_quoted(x):
                x = unquote(x)
        self.defval = x

    def jsonproto(self, jl):
        jl.append([self.vt, self.nm, self.defval, self.spec])
        if self.opt:
            jl[-1].append(True)
        while jl[-1][-1] is None:
            jl[-1].pop(-1)

#######################################################################


class ProtoType():
    def __init__(self, st, retval=True, prefix=""):
        self.st = st
        self.obj = None
        self.args = []
        self.argstruct = False
        wl = self.st.toks[1:]

        if retval:
            self.retval = CType(wl, st.vcc.enums)
        else:
            self.retval = CType(['VOID'], st.vcc.enums)

        self.bname = wl.pop(0)
        if not re.match("^[a-zA-Z.][a-zA-Z0-9_]*$", self.bname):
            err("%s(): Illegal name\n" % self.bname, warn=False)

        self.name = prefix + self.bname
        if not re.match('^[a-zA-Z_][a-zA-Z0-9_]*$', self.cname()):
            err("%s(): Illegal C-name\n" % self.cname(), warn=False)

        if len(wl) == 2 and wl[0] == '(' and wl[1] == ')':
            return

        if wl[0] != "(":
            err("Syntax error: Expected '(', got '%s'" % wl[0], warn=False)
        wl[0] = ','

        if wl[-1] != ")":
            err("Syntax error: Expected ')', got '%s'" % wl[-1], warn=False)
        wl[-1] = ','

        names = {}
        n = 0
        while wl:
            n += 1
            x = wl.pop(0)
            if x != ',':
                err("Expected ',' found '%s'" % x, warn=False)
            if not wl:
                break
            if wl[0] == '[':
                wl.pop(0)
                t = arg(wl, names, st.vcc.enums, ']')
                if t.nm is None:
                    err("Optional arguments must have names", warn=False)
                t.opt = True
                x = wl.pop(0)
                if x != ']':
                    err("Expected ']' found '%s'" % x, warn=False)
                self.argstruct = True
            else:
                t = arg(wl, names, st.vcc.enums, ',')
            if t.vt == 'VOID':
                err("arguments can not be of type '%s'" % t.vt, warn=False)
            if t.nm is None:
                t.nm2 = "arg%d" % n
            else:
                t.nm2 = t.nm
            self.args.append(t)

    def vcl_proto(self, terse, pfx=""):
        if isinstance(self.st, MethodStanza):
            pfx += pfx
        s = pfx
        if isinstance(self.st, ObjectStanza):
            s += "new " + self.obj + " = "
        elif self.retval is not None:
            s += self.retval.vcl() + " "

        if isinstance(self.st, ObjectStanza):
            s += self.st.vcc.modname + "." + self.name + "("
        elif isinstance(self.st, MethodStanza):
            s += self.obj + self.bname + "("
        else:
            s += self.name + "("
        ll = []
        for i in self.args:
            t = i.vcl(terse)
            if t in PRIVS:
                continue
            if i.nm is not None:
                t += " " + i.nm
            if not terse:
                if i.defval is not None:
                    t += "=" + i.defval
            if i.opt:
                t = "[" + t + "]"
            ll.append(t)
        t = ",@".join(ll)
        if len(s + t) > 68 and not terse:
            s += "\n" + pfx + pfx
            s += t.replace("@", "\n" + pfx + pfx)
            s += "\n" + pfx + ")"
        else:
            s += t.replace("@", " ") + ")"
        return s

    def rst_proto(self, fo, sep='-'):
        s = self.vcl_proto(False)
        if len(s) < 60:
            write_rst_hdr(fo, s, sep)
        else:
            s = self.vcl_proto(True)
            write_rst_hdr(fo, s, sep)
            fo.write("\n::\n\n" + self.vcl_proto(False, pfx="   ") + "\n")

    def cname(self, pfx=False):
        r = self.name.replace(".", "_")
        if pfx:
            return self.st.vcc.sympfx + r
        return r

    def proto(self, args, name):
        s = self.retval.ct + " " + name + '('
        ll = args
        if self.argstruct:
            ll.append(self.argstructname() + "*")
        else:
            for i in self.args:
                ll.append(i.ct)
        s += ", ".join(ll)
        return s + ');'

    def typedef_name(self):
        return 'td_' + self.st.vcc.sympfx + \
            self.st.vcc.modname + '_' + self.cname()

    def typedef(self, args):
        return "typedef " + self.proto(args, name=self.typedef_name())

    def argstructname(self):
        return "struct VARGS(%s)" % self.cname(False)

    def argstructure(self):
        s = "\n" + self.argstructname() + " {\n"
        for i in self.args:
            if i.opt:
                assert i.nm is not None
                s += "\tchar\t\t\tvalid_%s;\n" % i.nm
        for i in self.args:
            s += "\t" + i.ct
            if len(i.ct) < 8:
                s += "\t"
            if len(i.ct) < 16:
                s += "\t"
            s += "\t" + i.nm2 + ";\n"
        s += "};\n"
        return s

    def cproto(self, eargs, where):
        ''' Produce C language prototype '''
        s = ""
        if where == 'h':
            if self.argstruct:
                s += self.argstructure()
            s += lwrap(self.proto(eargs, self.cname(True)))
        elif where == 'c':
            s += lwrap(self.typedef(eargs))
        elif where == 'o':
            if self.argstruct:
                s += self.argstructure()
            s += lwrap(self.typedef(eargs))
        else:
            assert False
        return s

    def jsonproto(self, jl, cfunc):
        ''' Produce VCL prototype as JSON '''
        ll = []
        self.retval.jsonproto(ll)
        ll.append('%s.%s' % (self.st.vcc.csn, cfunc))
        if self.argstruct:
            # We cannot use VARGS() here, we are after the #undef
            ll.append('struct arg_%s%s_%s' %
                (self.st.vcc.sympfx, self.st.vcc.modname, self.cname(False)))
        else:
            ll.append("")
        for i in self.args:
            i.jsonproto(ll)
        jl.append(ll)

#######################################################################


class Stanza():

    ''' Base class for all $-Stanzas '''

    def __init__(self, vcc, toks, doc):
        self.toks = toks
        doc = doc.split('\n')
        while doc and not doc[0].strip():
            doc.pop(0)
        while doc and not doc[-1].strip():
            doc.pop(-1)
        self.doc = doc
        self.vcc = vcc
        self.rstlbl = None
        self.null_ok = False
        self.methods = None
        self.proto = None
        self.parse()

    def parse(self):
        assert "subclass should have defined" == "parse method"

    def syntax(self):
        err("Syntax error.\n" +
            "\tShould be: " + self.__doc__.strip() + "\n" +
            "\tIs: " + " ".join(self.toks) + "\n",
            warn=False)

    def rstfile(self, fo, man):
        self.rsthead(fo, man)
        self.rstdoc(fo, man)

    def rsthead(self, fo, unused_man):
        ''' Emit the systematic part of the documentation '''
        if self.rstlbl:
            fo.write('\n.. _' + self.rstlbl + ':\n')
        if self.proto:
            self.proto.rst_proto(fo)
            fo.write("\n")

    def rstdoc(self, fo, unused_man):
        ''' Emit the explanatory part of the documentation '''
        fo.write("\n".join(self.doc) + "\n")

    def synopsis(self, fo, man):
        if man and self.proto:
            fo.write(self.proto.vcl_proto(True, pfx="  ") + '\n  \n')
        elif self.proto and self.rstlbl:
            fo.write('  :ref:`%s`\n   \n' % self.rstlbl)

    def cstuff(self, unused_fo, unused_where):
        return

    def fmt_cstruct_proto(self, fo, proto, define):
        if define:
            fmt_cstruct(
                fo,
                proto.typedef_name(),
                '*' + proto.cname() + ';'
            )
        else:
            fmt_cstruct(
                fo,
                '.' + proto.cname() + ' =',
                self.vcc.sympfx + proto.cname() + ','
            )

    def cstruct(self, unused_fo, unused_define):
        return

    def json(self, unused_jl):
        ''' Add to the json we hand VCC '''
        return

#######################################################################


class ModuleStanza(Stanza):

    ''' $Module modname man_section description ... '''

    def parse(self):
        if len(self.toks) < 4:
            self.syntax()
        self.vcc.modname = self.toks[1]
        self.vcc.mansection = self.toks[2]
        if len(self.toks) == 4 and is_quoted(self.toks[3]):
            self.vcc.moddesc = unquote(self.toks[3])
        else:
            print("\nNOTICE: Please put $Module description in quotes.\n")
            self.vcc.moddesc = " ".join(self.toks[3:])
        self.rstlbl = "vmod_%s(%d)" % (self.vcc.modname, 3)
        self.vcc.contents.append(self)

    def rsthead(self, fo, man):

        if man:
            write_rst_hdr(fo, "vmod_" + self.vcc.modname, "=", "=")
            write_rst_hdr(fo, self.vcc.moddesc, "-", "-")
            fo.write("\n")
            fo.write(":Manual section: " + self.vcc.mansection + "\n")
        else:
            if self.rstlbl:
                fo.write('\n.. _' + self.rstlbl + ':\n')
            write_rst_hdr(fo,
                          "VMOD " + self.vcc.modname +
                          ' - ' + self.vcc.moddesc,
                          "=", "=")

        if self.vcc.auto_synopsis:
            write_rst_hdr(fo, "SYNOPSIS", "=")
            fo.write("\n")
            fo.write(".. parsed-literal::\n\n")
            fo.write('  import %s [as name] [from "path"]\n' % self.vcc.modname)
            fo.write("  \n")
            for c in self.vcc.contents:
                c.synopsis(fo, man)

class ABIStanza(Stanza):

    ''' $ABI [strict|vrt] '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        self.vcc.strict_abi = {
            'strict': True,
            'vrt': False,
        }.get(self.toks[1])
        if self.vcc.strict_abi is None:
            err("Valid ABI types are 'strict' or 'vrt', got '%s'\n" %
                self.toks[1])
        self.vcc.contents.append(self)
        if not self.vcc.strict_abi:
            self.vcc.vrt_major = "VRT_MAJOR_VERSION"
            self.vcc.vrt_minor = "VRT_MINOR_VERSION"

class PrefixStanza(Stanza):

    ''' $Prefix symbol '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        self.vcc.sympfx = self.toks[1] + "_"
        self.vcc.contents.append(self)


class SynopsisStanza(Stanza):

    ''' $Synopsis [auto|manual] '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        valid = {
            'auto': True,
            'manual': False,
        }
        self.vcc.auto_synopsis = valid.get(self.toks[1])
        if self.vcc.auto_synopsis is None:
            err("Valid Synopsis values are 'auto' or 'manual', got '%s'\n" %
                self.toks[1])
        self.vcc.contents.append(self)


class EventStanza(Stanza):

    ''' $Event function_name '''

    def parse(self):
        if len(self.toks) != 2:
            self.syntax()
        self.event_func = self.toks[1]
        self.vcc.contents.append(self)

    def rstfile(self, fo, man):
        if self.doc:
            err("Not emitting .RST for $Event %s\n" %
                self.event_func)

    def cstuff(self, fo, where):
        if where == 'h':
            fo.write("vmod_event_f VPFX(%s);\n" % self.event_func)

    def cstruct(self, fo, define):
        if define:
            fmt_cstruct(fo, "vmod_event_f", "*_event;")
        else:
            fmt_cstruct(fo,
                        "._event =",
                        self.vcc.sympfx + self.event_func + ',')

    def json(self, jl):
        jl.append(["$EVENT", "%s._event" % self.vcc.csn])


class FunctionStanza(Stanza):

    ''' $Function TYPE name ( ARGUMENTS ) '''

    def parse(self):
        self.proto = ProtoType(self)
        self.rstlbl = '%s.%s()' % (self.vcc.modname, self.proto.name)
        self.vcc.contents.append(self)

    def cstuff(self, fo, where):
        fo.write(self.proto.cproto(['VRT_CTX'], where))

    def cstruct(self, fo, define):
        self.fmt_cstruct_proto(fo, self.proto, define)

    def json(self, jl):
        jl.append(["$FUNC", "%s" % self.proto.name])
        self.proto.jsonproto(jl[-1], self.proto.cname())


class ObjectStanza(Stanza):

    ''' $Object TYPE class ( ARGUMENTS ) '''

    def parse(self):
        if self.toks[1] == "NULL_OK":
            self.toks.pop(1)
            self.null_ok = True
        self.proto = ProtoType(self, retval=False)
        self.proto.obj = "x" + self.proto.name

        self.init = copy.copy(self.proto)
        self.init.name += '__init'

        self.fini = copy.copy(self.proto)
        self.fini.name += '__fini'
        self.fini.argstruct = False
        self.fini.args = []

        self.rstlbl = '%s.%s()' % (self.vcc.modname, self.proto.name)
        self.vcc.contents.append(self)
        self.methods = []

    def rsthead(self, fo, man):
        if self.rstlbl:
            fo.write('\n.. _' + self.rstlbl + ':\n')
        self.proto.rst_proto(fo)
        fo.write("\n" + "\n".join(self.doc) + "\n")
        for i in self.methods:
            i.rstfile(fo, man)

    def rstdoc(self, unused_fo, unused_man):
        return

    def synopsis(self, fo, man):
        if man and self.proto:
            fo.write(self.proto.vcl_proto(True, pfx="  ") + '\n  \n')
            for i in self.methods:
                if i.proto:
                    fo.write(i.proto.vcl_proto(True, pfx="   ") + '\n   \n')
        elif self.proto and self.rstlbl:
            fo.write('  :ref:`%s`\n  \n' % self.rstlbl)
            for i in self.methods:
                if i.proto and i.rstlbl:
                    fo.write('      :ref:`%s`\n  \n' % i.rstlbl)

    def cstuff(self, fo, w):
        sn = 'VPFX(' + self.vcc.modname + '_' + self.proto.name + ')'
        fo.write("struct %s;\n" % sn)

        fo.write(self.init.cproto(
            ['VRT_CTX', 'struct %s **' % sn, 'const char *'], w))
        fo.write(self.fini.cproto(['struct %s **' % sn], w))
        for i in self.methods:
            fo.write(i.proto.cproto(['VRT_CTX', 'struct %s *' % sn], w))
        fo.write("\n")

    def cstruct(self, fo, define):
        self.fmt_cstruct_proto(fo, self.init, define)
        self.fmt_cstruct_proto(fo, self.fini, define)
        for i in self.methods:
            i.cstruct(fo, define)
        fo.write("\n")

    def json(self, jl):
        ll = [
            "$OBJ",
            self.proto.name,
            {"NULL_OK": self.null_ok},
            "struct %s%s_%s" %
            (self.vcc.sympfx, self.vcc.modname, self.proto.name),
        ]

        l2 = ["$INIT"]
        ll.append(l2)
        self.init.jsonproto(l2, self.init.name)

        l2 = ["$FINI"]
        ll.append(l2)
        self.fini.jsonproto(l2, self.fini.name)

        for i in self.methods:
            i.json(ll)

        jl.append(ll)

#######################################################################


class MethodStanza(Stanza):

    ''' $Method TYPE . method ( ARGUMENTS ) '''

    def parse(self):
        p = self.vcc.contents[-1]
        assert isinstance(p, ObjectStanza)
        self.pfx = p.proto.name
        self.proto = ProtoType(self, prefix=self.pfx)
        if not self.proto.bname.startswith("."):
            err("$Method %s: Method names need to start with . (dot)"
                % self.proto.bname, warn=False)
        self.proto.obj = "x" + self.pfx
        self.rstlbl = 'x%s()' % self.proto.name
        p.methods.append(self)

    def cstruct(self, fo, define):
        self.fmt_cstruct_proto(fo, self.proto, define)

    def json(self, jl):
        jl.append(["$METHOD", self.proto.name[len(self.pfx)+1:]])
        self.proto.jsonproto(jl[-1], self.proto.cname())


class AliasStanza(Stanza):

    ''' $Alias ALIAS SYMBOL '''

    def find_symbol(self, tbl, name):
        for sym in tbl:
            if sym.proto is None:
                continue
            if sym.proto.name == name:
                return sym
        err("Symbol '%s' not found\n" % name, warn=False)

    def parse(self):
        if len(self.toks) != 3:
            err("Syntax error, expected: $Alias <alias> <symbol>\n", warn=False)
        if not re.match('^\.?[a-zA-Z_][a-zA-Z0-9_]*$', self.toks[1]):
            err("%s(): Illegal C-name\n" % self.toks[1], warn=False)
        if self.toks[1][0] == '.':
            if not re.match('^[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*$',
                    self.toks[2]):
                err("Syntax error, expected: $Alias <.alias> <obj.method>\n",
                        warn=False)
            obj_name = self.toks[2].split('.')[0]
            obj = self.find_symbol(self.vcc.contents, obj_name)
            self.find_symbol(obj.methods, self.toks[2])
            self.sym_alias = "%s%s" % (obj_name, self.toks[1])
            self.doc_alias = "x%s" % self.sym_alias
            self.sym_name = self.toks[2]
            self.doc_name = "x%s" % self.toks[2]
        else:
            self.find_symbol(self.vcc.contents, self.toks[2])
            self.sym_alias = self.toks[1]
            self.doc_alias = self.toks[1]
            self.sym_name = self.toks[2]
            self.doc_name = self.toks[2]
        self.vcc.contents.append(self)

    def rsthead(self, fo, unused_man):
        write_rst_hdr(fo, "ALIAS %s()" % self.doc_alias, "-")
        fo.write("\nDeprecated alias for ``%s()``." % self.doc_name)
        if len(self.doc) > 0:
            fo.write("\n\n")

    def json(self, jl):
        jl.append(["$ALIAS", self.sym_alias, self.sym_name])


#######################################################################

DISPATCH = {
    "Module":   ModuleStanza,
    "Prefix":   PrefixStanza,
    "ABI":      ABIStanza,
    "Event":    EventStanza,
    "Function": FunctionStanza,
    "Object":   ObjectStanza,
    "Method":   MethodStanza,
    "Synopsis": SynopsisStanza,
    "Alias":    AliasStanza,
}


class vcc():

    ''' Processing context for a single .vcc file '''

    def __init__(self, inputvcc, rstdir, outputprefix):
        self.inputfile = inputvcc
        self.rstdir = rstdir
        self.pfx = outputprefix
        self.sympfx = "vmod_"
        self.contents = []
        self.commit_files = []
        self.copyright = ""
        self.enums = {}
        self.strict_abi = True
        self.vrt_major = "0"
        self.vrt_minor = "0"
        self.auto_synopsis = True
        self.modname = None
        self.csn = None

    def openfile(self, fn):
        self.commit_files.append(fn)
        return open(fn + ".tmp", "w")

    def commit(self):
        for i in self.commit_files:
            os.rename(i + ".tmp", i)

    def parse(self):
        global inputline
        b = open(self.inputfile, "rb").read()
        a = "\n" + b.decode("utf-8")
        h = hashlib.sha256()
        s = a.split("\n$")
        self.copyright = s.pop(0).strip()
        while s:
            ss = re.split('\n([^\t ])', s.pop(0), maxsplit=1)
            toks = self.tokenize(ss[0])
            inputline = '$' + ' '.join(toks)
            h.update((inputline + '\n').encode('utf-8'))
            docstr = "".join(ss[1:])
            stanzaclass = DISPATCH.get(toks[0])
            if stanzaclass is None:
                err("Unknown stanza $%s" % toks[0], warn=False)
            stanzaclass(self, toks, docstr)
            inputline = None
        self.csn = "Vmod_%s%s_Func" % (self.sympfx, self.modname)
        self.file_id = h.hexdigest()

    def tokenize(self, txt, seps=None, quotes=None):
        if seps is None:
            seps = "[](){},="
        if quotes is None:
            quotes = '"' + "'"
        quote = None
        out = []
        i = 0
        inside = False
        while i < len(txt):
            c = txt[i]
            # print("T", [c], quote, inside, i)
            i += 1
            if quote is not None and c == quote:
                inside = False
                quote = None
                out[-1] += c
            elif quote is not None:
                out[-1] += c
            elif c.isspace():
                inside = False
            elif seps.find(c) >= 0:
                inside = False
                out.append(c)
            elif quotes.find(c) >= 0:
                quote = c
                out.append(c)
            elif inside:
                out[-1] += c
            else:
                out.append(c)
                inside = True
        #print("TOK", [str])
        #for i in out:
        #    print("\t", [i])
        return out

    def rstfile(self, man=False):
        ''' Produce rst documentation '''
        fn = os.path.join(self.rstdir, "vmod_" + self.modname)
        if man:
            fn += ".man"
        fn += ".rst"
        fo = self.openfile(fn)
        write_rst_file_warning(fo, self.inputfile)
        if man:
            fo.write(".. role:: ref(emphasis)\n")
        else:
            fo.write('\n:tocdepth: 1\n')

        for i in self.contents:
            i.rstfile(fo, man)

        if self.copyright:
            write_rst_hdr(fo, "COPYRIGHT", "=")
            fo.write("\n::\n\n")
            a = self.copyright
            a = a.replace("\n#", "\n ")
            if a[:2] == "#\n":
                a = a[2:]
            if a[:3] == "#-\n":
                a = a[3:]
            fo.write(a + "\n")

        fo.close()

    def amboilerplate(self):
        ''' Produce boilplate for autocrap tools '''
        vcc = os.path.basename(self.inputfile)
        src = glob.glob("vmod_" + self.modname + "*.[ch]")
        src.sort()
        fn = "automake_boilerplate_" + self.modname + ".am"
        fo = self.openfile(fn)
        fo.write(AMBOILERPLATE.replace("XXX", self.modname)
                 .replace("VCC", vcc)
                 .replace("PFX", self.pfx)
                 .replace("SRC", " \\\n\t".join(src)))
        fo.close()

    def mkdefs(self, fo):
        fo.write('#define VPFX(a) %s##a\n' % self.sympfx)
        fo.write('#define VARGS(a) arg_%s%s_##a\n' %
            (self.sympfx, self.modname))
        fo.write('#define VENUM(a) enum_%s%s_##a\n' %
            (self.sympfx, self.modname))
        for a in ('VPFX', 'VARGS', 'VENUM'):
            for b in (755, 767):
                fo.write('//lint -esym(%d, %s)\n' % (b, a))
        fo.write('//lint -esym(755, VARGS)\n')
        fo.write('//lint -esym(755, VENUM)\n')
        fo.write('\n')

    def mkhfile(self):
        ''' Produce vcc_if.h file '''
        fn = self.pfx + ".h"
        fo = self.openfile(fn)
        write_c_file_warning(fo, self.inputfile)
        fo.write("#ifndef VDEF_H_INCLUDED\n")
        fo.write('#  error "Include vdef.h first"\n')
        fo.write("#endif\n")
        fo.write("#ifndef VRT_H_INCLUDED\n")
        fo.write('#  error "Include vrt.h first"\n')
        fo.write("#endif\n")
        fo.write("\n")

        self.mkdefs(fo)

        for j in sorted(self.enums):
            fo.write("extern VCL_ENUM VENUM(%s);\n" % j)
        fo.write("\n")
        for j in sorted(self.enums):
            fo.write("//lint -esym(14, enum_%s%s_%s)\n" %
                (self.sympfx, self.modname, j))
            fo.write("//lint -esym(759, enum_%s%s_%s)\n" %
                (self.sympfx, self.modname, j))
            fo.write("//lint -esym(765, enum_%s%s_%s)\n" %
                (self.sympfx, self.modname, j))
        fo.write("\n")

        for j in self.contents:
            j.cstuff(fo, 'h')
        fo.close()

    def cstruct(self, fo):
        fo.write("\nstruct %s {\n" % self.csn)
        for j in self.contents:
            j.cstruct(fo, True)
        for j in sorted(self.enums):
            fmt_cstruct(fo, 'VCL_ENUM', '*enum_%s;' % j)
        fo.write("};\n")

    def cstruct_init(self, fo):
        fo.write("\nstatic const struct %s %s = {\n" % (self.csn, self.csn))
        for j in self.contents:
            j.cstruct(fo, False)
        fo.write("\n")
        for j in sorted(self.enums):
            fmt_cstruct(fo, '.enum_%s =' % j, '&VENUM(%s),' % j)
        fo.write("};\n")

    def iter_json(self, fnx):

        jl = [["$VMOD", "1.0", self.modname, self.csn, self.file_id]]
        jl.append(["$CPROTO"])
        for i in open(fnx):
            jl[-1].append(i.rstrip())
        jl[-1].append("static struct %s %s;" % (self.csn, self.csn))
        for j in self.contents:
            j.json(jl)
        for i in json.dumps(jl, indent=2, separators=(",", ": ")).splitlines():
            yield i + " "

    def json(self, fo, fnx):
        fo.write('#define STRINGIFY3(arg) #arg\n')
        fo.write('#define STRINGIFY2(arg) STRINGIFY3(#arg)\n')
        fo.write('#define STRINGIFY1(arg) STRINGIFY2(arg)\n')
        fo.write("\nstatic const char Vmod_Json[] = {\n")
        fo.write('\t"VMOD_JSON_SPEC\x02"\n')

        for n, i in enumerate(self.iter_json(fnx)):
            fo.write('\t"')
            for j in i:
                if j in '"\\':
                    fo.write('\\')
                fo.write(j)
            if n == 6:
                # Hand-munge the JSON to insert stuff only known by
                # the C-compiler at compile-time.
                fo.write(',"\n\t"    \\"" VMOD_ABI_Version "\\", "\n')
                fo.write('\t    STRINGIFY1(%s) ", "\n' % self.vrt_major)
                fo.write('\t    STRINGIFY1(%s)\n' % self.vrt_minor)
            else:
                fo.write('"\n')
        fo.write('\t\"\\n\\x03\"\n};\n')
        fo.write('#undef STRINGIFY\n')


    def vmod_data(self, fo):
        vmd = "Vmod_%s_Data" % self.modname
        fo.write('\n')
        for i in (714, 759, 765):
            fo.write("/*lint -esym(%d, %s) */\n" % (i, vmd))
        fo.write("\nextern const struct vmod_data %s;\n" % vmd)
        fo.write("\nconst struct vmod_data %s = {\n" % vmd)
        fo.write("\t.vrt_major =\t%s,\n" % self.vrt_major)
        fo.write("\t.vrt_minor =\t%s,\n" % self.vrt_minor)
        fo.write('\t.name =\t\t"%s",\n' % self.modname)
        fo.write('\t.func =\t\t&%s,\n' % self.csn)
        fo.write('\t.func_len =\tsizeof(%s),\n' % self.csn)
        fo.write('\t.func_name =\t"%s",\n' % self.csn)
        fo.write('\t.json =\t\tVmod_Json,\n')
        fo.write('\t.abi =\t\tVMOD_ABI_Version,\n')
        fo.write("\t.file_id =\t\"%s\",\n" % self.file_id)
        fo.write("};\n")

    def mkcfile(self):
        ''' Produce vcc_if.c file '''
        fno = self.pfx + ".c"
        fo = self.openfile(fno)
        fnx = fno + ".tmp2"
        fx = open(fnx, "w")

        write_c_file_warning(fo, self.inputfile)

        self.mkdefs(fx)

        fo.write('#include "config.h"\n')
        for i in ["vdef", "vrt", self.pfx, "vmod_abi"]:
            fo.write('#include "%s.h"\n' % i)
        fo.write("\n")

        for j in sorted(self.enums):
            fo.write('VCL_ENUM VENUM(%s) = "%s";\n' % (j, j))
        fo.write("\n")

        for i in self.contents:
            if isinstance(i, ObjectStanza):
                i.cstuff(fo, 'c')
                i.cstuff(fx, 'o')

        fx.write("/* Functions */\n")
        for i in self.contents:
            if isinstance(i, FunctionStanza):
                i.cstuff(fo, 'c')
                i.cstuff(fx, 'o')

        self.cstruct(fo)
        self.cstruct(fx)

        fo.write("\n/*lint -esym(754, " + self.csn + "::*) */\n")
        self.cstruct_init(fo)

        fx.write('#undef VPFX\n')
        fx.write('#undef VARGS\n')
        fx.write('#undef VENUM\n')

        fx.close()

        self.json(fo, fnx)

        self.vmod_data(fo)

        os.remove(fnx)

        fo.close()

#######################################################################


def runmain(inputvcc, rstdir, outputprefix):

    v = vcc(inputvcc, rstdir, outputprefix)
    v.parse()

    v.rstfile(man=False)
    v.rstfile(man=True)
    v.mkhfile()
    v.mkcfile()
    if opts.boilerplate:
        v.amboilerplate()

    v.commit()


if __name__ == "__main__":
    usagetext = "Usage: %prog [options] <vmod.vcc>"
    oparser = optparse.OptionParser(usage=usagetext)

    oparser.add_option('-b', '--boilerplate', action='store_true',
                       default=False,
                       help="Create automake_boilerplate.am")
    oparser.add_option('-N', '--strict', action='store_true', default=False,
                       help="Be strict when parsing the input file")
    oparser.add_option('-o', '--output', metavar="prefix", default='vcc_if',
                       help='Output file prefix (default: "vcc_if")')
    oparser.add_option('-w', '--rstdir', metavar="directory", default='.',
                       help='Where to save the generated RST files ' +
                       '(default: ".")')
    (opts, args) = oparser.parse_args()

    i_vcc = None
    for f in args:
        if os.path.exists(f):
            i_vcc = f
            break
    if i_vcc is None and os.path.exists("vmod.vcc"):
        i_vcc = "vmod.vcc"
    if i_vcc is None:
        print("ERROR: No vmod.vcc file supplied or found.", file=sys.stderr)
        oparser.print_help()
        exit(-1)

    runmain(i_vcc, opts.rstdir, opts.output)
