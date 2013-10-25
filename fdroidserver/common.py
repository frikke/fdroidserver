# -*- coding: utf-8 -*-
#
# common.py - part of the FDroid server tools
# Copyright (C) 2010-13, Ciaran Gultnieks, ciaran@ciarang.com
# Copyright (C) 2013 Daniel Martí <mvdan@mvdan.cc>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import glob, os, sys, re
import shutil
import subprocess
import time
import operator
import cgi
import Queue
import threading
import magic

def getvcs(vcstype, remote, local, sdk_path):
    if vcstype == 'git':
        return vcs_git(remote, local, sdk_path)
    if vcstype == 'svn':
        return vcs_svn(remote, local, sdk_path)
    if vcstype == 'git-svn':
        return vcs_gitsvn(remote, local, sdk_path)
    if vcstype == 'hg':
        return vcs_hg(remote, local, sdk_path)
    if vcstype == 'bzr':
        return vcs_bzr(remote, local, sdk_path)
    if vcstype == 'srclib':
        if local != 'build/srclib/' + remote:
            raise VCSException("Error: srclib paths are hard-coded!")
        return getsrclib(remote, 'build/srclib', sdk_path, raw=True)
    raise VCSException("Invalid vcs type " + vcstype)

def getsrclibvcs(name):
    srclib_path = os.path.join('srclibs', name + ".txt")
    if not os.path.exists(srclib_path):
        raise VCSException("Missing srclib " + name)
    return parse_srclib(srclib_path)['Repo Type']

class vcs:
    def __init__(self, remote, local, sdk_path):

        self.sdk_path = sdk_path

        # It's possible to sneak a username and password in with
        # the remote address for svn...
        self.username = None
        if self.repotype() == 'svn':
            index = remote.find('@')
            if index != -1:
                self.username = remote[:index]
                remote = remote[index+1:]
                index = self.username.find(':')
                if index == -1:
                    raise VCSException("Password required with username")
                self.password = self.username[index+1:]
                self.username = self.username[:index]

        self.remote = remote
        self.local = local
        self.refreshed = False
        self.srclib = None

    # Take the local repository to a clean version of the given revision, which
    # is specificed in the VCS's native format. Beforehand, the repository can
    # be dirty, or even non-existent. If the repository does already exist
    # locally, it will be updated from the origin, but only once in the
    # lifetime of the vcs object.
    # None is acceptable for 'rev' if you know you are cloning a clean copy of
    # the repo - otherwise it must specify a valid revision.
    def gotorevision(self, rev):

        # The .fdroidvcs-id file for a repo tells us what VCS type
        # and remote that directory was created from, allowing us to drop it
        # automatically if either of those things changes.
        fdpath = os.path.join(self.local, '..',
                '.fdroidvcs-' + os.path.basename(self.local))
        cdata = self.repotype() + ' ' + self.remote
        writeback = True
        deleterepo = False
        if os.path.exists(self.local):
            if os.path.exists(fdpath):
                with open(fdpath, 'r') as f:
                    fsdata = f.read()
                if fsdata == cdata:
                    writeback = False
                else:
                    deleterepo = True
                    print "*** Repository details changed - deleting ***"
            else:
                deleterepo = True
                print "*** Repository details missing - deleting ***"
        if deleterepo:
            shutil.rmtree(self.local)

        self.gotorevisionx(rev)

        # If necessary, write the .fdroidvcs file.
        if writeback:
            with open(fdpath, 'w') as f:
                f.write(cdata)

    # Derived classes need to implement this. It's called once basic checking
    # has been performend.
    def gotorevisionx(self, rev):
        raise VCSException("This VCS type doesn't define gotorevisionx")

    # Initialise and update submodules
    def initsubmodules(self):
        raise VCSException('Submodules not supported for this vcs type')

    # Get a list of all known tags
    def gettags(self):
        raise VCSException('gettags not supported for this vcs type')

    # Get current commit reference (hash, revision, etc)
    def getref(self):
        raise VCSException('getref not supported for this vcs type')

    # Returns the srclib (name, path) used in setting up the current
    # revision, or None.
    def getsrclib(self):
        return self.srclib

class vcs_git(vcs):

    def repotype(self):
        return 'git'

    # If the local directory exists, but is somehow not a git repository, git
    # will traverse up the directory tree until it finds one that is (i.e.
    # fdroidserver) and then we'll proceed to destroy it! This is called as
    # a safety check.
    def checkrepo(self):
        p = subprocess.Popen(['git', 'rev-parse', '--show-toplevel'],
                stdout=subprocess.PIPE, cwd=self.local)
        result = p.communicate()[0].rstrip()
        if not result.endswith(self.local):
            raise VCSException('Repository mismatch')

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            # Brand new checkout...
            if subprocess.call(['git', 'clone', self.remote, self.local]) != 0:
                raise VCSException("Git clone failed")
            self.checkrepo()
        else:
            self.checkrepo()
            # Discard any working tree changes...
            if subprocess.call(['git', 'reset', '--hard'], cwd=self.local) != 0:
                raise VCSException("Git reset failed")
            # Remove untracked files now, in case they're tracked in the target
            # revision (it happens!)...
            if subprocess.call(['git', 'clean', '-dffx'], cwd=self.local) != 0:
                raise VCSException("Git clean failed")
            if not self.refreshed:
                # Get latest commits and tags from remote...
                if subprocess.call(['git', 'fetch', 'origin'],
                        cwd=self.local) != 0:
                    raise VCSException("Git fetch failed")
                if subprocess.call(['git', 'fetch', '--tags', 'origin'],
                        cwd=self.local) != 0:
                    raise VCSException("Git fetch failed")
                self.refreshed = True
        # Check out the appropriate revision...
        rev = str(rev if rev else 'origin/master')
        if subprocess.call(['git', 'checkout', rev], cwd=self.local) != 0:
            raise VCSException("Git checkout failed")
        # Get rid of any uncontrolled files left behind...
        if subprocess.call(['git', 'clean', '-dffx'], cwd=self.local) != 0:
            raise VCSException("Git clean failed")

    def initsubmodules(self):
        self.checkrepo()
        if subprocess.call(['git', 'submodule', 'init'],
                cwd=self.local) != 0:
            raise VCSException("Git submodule init failed")
        if subprocess.call(['git', 'submodule', 'update'],
                cwd=self.local) != 0:
            raise VCSException("Git submodule update failed")

    def gettags(self):
        self.checkrepo()
        p = subprocess.Popen(['git', 'tag'],
                stdout=subprocess.PIPE, cwd=self.local)
        return p.communicate()[0].splitlines()


class vcs_gitsvn(vcs):

    def repotype(self):
        return 'git-svn'

    # If the local directory exists, but is somehow not a git repository, git
    # will traverse up the directory tree until it finds one that is (i.e.
    # fdroidserver) and then we'll proceed to destory it! This is called as
    # a safety check.
    def checkrepo(self):
        p = subprocess.Popen(['git', 'rev-parse', '--show-toplevel'],
                stdout=subprocess.PIPE, cwd=self.local)
        result = p.communicate()[0].rstrip()
        if not result.endswith(self.local):
            raise VCSException('Repository mismatch')

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            # Brand new checkout...
            gitsvn_cmd = ['git', 'svn', 'clone']
            remote_split = self.remote.split(';')
            if len(remote_split) > 1:
                for i in remote_split[1:]:
                    if i.startswith('trunk='):
                        gitsvn_cmd += ['-T', i[6:]]
                    elif i.startswith('tags='):
                        gitsvn_cmd += ['-t', i[5:]]
                    elif i.startswith('branches='):
                        gitsvn_cmd += ['-b', i[9:]]
                if subprocess.call(gitsvn_cmd + [remote_split[0], self.local]) != 0:
                    raise VCSException("Git clone failed")
            else:
                if subprocess.call(gitsvn_cmd + [self.remote, self.local]) != 0:
                    raise VCSException("Git clone failed")
            self.checkrepo()
        else:
            self.checkrepo()
            # Discard any working tree changes...
            if subprocess.call(['git', 'reset', '--hard'], cwd=self.local) != 0:
                raise VCSException("Git reset failed")
            # Remove untracked files now, in case they're tracked in the target
            # revision (it happens!)...
            if subprocess.call(['git', 'clean', '-dffx'], cwd=self.local) != 0:
                raise VCSException("Git clean failed")
            if not self.refreshed:
                # Get new commits and tags from repo...
                if subprocess.call(['git', 'svn', 'rebase'],
                        cwd=self.local) != 0:
                    raise VCSException("Git svn rebase failed")
                self.refreshed = True

        rev = str(rev if rev else 'master')
        if rev:
            nospaces_rev = rev.replace(' ', '%20')
            # Try finding a svn tag
            p = subprocess.Popen(['git', 'checkout', 'tags/' + nospaces_rev],
                    cwd=self.local, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate()
            if p.returncode == 0:
                print out
            else:
                # No tag found, normal svn rev translation
                # Translate svn rev into git format
                p = subprocess.Popen(['git', 'svn', 'find-rev', 'r' + rev],
                    cwd=self.local, stdout=subprocess.PIPE)
                git_rev = p.communicate()[0].rstrip()
                if p.returncode != 0 or len(git_rev) == 0:
                    # Try a plain git checkout as a last resort
                    p = subprocess.Popen(['git', 'checkout', rev], cwd=self.local,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    out, err = p.communicate()
                    if p.returncode == 0:
                        print out
                    else:
                        raise VCSException("No git treeish found and direct git checkout failed")
                else:
                    # Check out the git rev equivalent to the svn rev
                    p = subprocess.Popen(['git', 'checkout', git_rev], cwd=self.local,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    out, err = p.communicate()
                    if p.returncode == 0:
                        print out
                    else:
                        raise VCSException("Git svn checkout failed")
        # Get rid of any uncontrolled files left behind...
        if subprocess.call(['git', 'clean', '-dffx'], cwd=self.local) != 0:
            raise VCSException("Git clean failed")

    def gettags(self):
        self.checkrepo()
        return os.listdir(os.path.join(self.local, '.git/svn/refs/remotes/tags'))

    def getref(self):
        self.checkrepo()
        p = subprocess.Popen(['git', 'svn', 'find-rev', 'HEAD'],
                stdout=subprocess.PIPE, cwd=self.local)
        return p.communicate()[0]

class vcs_svn(vcs):

    def repotype(self):
        return 'svn'

    def userargs(self):
        if self.username is None:
            return ['--non-interactive']
        return ['--username', self.username, 
                '--password', self.password,
                '--non-interactive']

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            if subprocess.call(['svn', 'checkout', self.remote, self.local] +
                    self.userargs()) != 0:
                raise VCSException("Svn checkout failed")
        else:
            for svncommand in (
                    'svn revert -R .',
                    r"svn status | awk '/\?/ {print $2}' | xargs rm -rf"):
                if subprocess.call(svncommand, cwd=self.local, shell=True) != 0:
                    raise VCSException("Svn reset ({0}) failed in {1}".format(svncommand, self.local))
            if not self.refreshed:
                if subprocess.call(['svn', 'update'] +
                        self.userargs(), cwd=self.local) != 0:
                    raise VCSException("Svn update failed")
                self.refreshed = True

        revargs = list(['-r', rev] if rev else [])
        if subprocess.call(['svn', 'update', '--force'] + revargs +
                self.userargs(), cwd=self.local) != 0:
            raise VCSException("Svn update failed")

    def getref(self):
        p = subprocess.Popen(['svn', 'info'],
                stdout=subprocess.PIPE, cwd=self.local)
        for line in p.communicate()[0].splitlines():
            if line is not None and line.startswith('Last Changed Rev: '):
                return line[18:]

class vcs_hg(vcs):

    def repotype(self):
        return 'hg'

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            if subprocess.call(['hg', 'clone', self.remote, self.local]) !=0:
                raise VCSException("Hg clone failed")
        else:
            if subprocess.call('hg status -u | xargs rm -rf',
                    cwd=self.local, shell=True) != 0:
                raise VCSException("Hg clean failed")
            if not self.refreshed:
                if subprocess.call(['hg', 'pull'],
                        cwd=self.local) != 0:
                    raise VCSException("Hg pull failed")
                self.refreshed = True

        rev = str(rev if rev else 'default')
        if rev:
            revargs = [rev]
            if subprocess.call(['hg', 'checkout', '-C'] + revargs,
                    cwd=self.local) != 0:
                raise VCSException("Hg checkout failed")

    def gettags(self):
        p = subprocess.Popen(['hg', 'tags', '-q'],
                stdout=subprocess.PIPE, cwd=self.local)
        return p.communicate()[0].splitlines()[1:]


class vcs_bzr(vcs):

    def repotype(self):
        return 'bzr'

    def gotorevisionx(self, rev):
        if not os.path.exists(self.local):
            if subprocess.call(['bzr', 'branch', self.remote, self.local]) != 0:
                raise VCSException("Bzr branch failed")
        else:
            if subprocess.call(['bzr', 'clean-tree', '--force',
                    '--unknown', '--ignored'], cwd=self.local) != 0:
                raise VCSException("Bzr revert failed")
            if not self.refreshed:
                if subprocess.call(['bzr', 'pull'],
                        cwd=self.local) != 0:
                    raise VCSException("Bzr update failed")
                self.refreshed = True

        revargs = list(['-r', rev] if rev else [])
        if subprocess.call(['bzr', 'revert'] + revargs,
                cwd=self.local) != 0:
            raise VCSException("Bzr revert failed")

    def __init__(self, remote, local, sdk_path):

        self.sdk_path = sdk_path

        index = remote.find('@')
        if index != -1:
            self.username = remote[:index]
            remote = remote[index+1:]
            index = self.username.find(':')
            if index == -1:
                raise VCSException("Password required with username")
            self.password = self.username[index+1:]
            self.username = self.username[:index]
        else:
            self.username = None

        self.remote = remote
        self.local = local
        self.refreshed = False
        self.srclib = None


# Get the type expected for a given metadata field.
def metafieldtype(name):
    if name == 'Description':
        return 'multiline'
    if name == 'Requires Root':
        return 'flag'
    if name == 'Build Version':
        return 'build'
    if name == 'Use Built':
        return 'obsolete'
    return 'string'


# Parse metadata for a single application.
#
#  'metafile' - the filename to read. The package id for the application comes
#               from this filename. Pass None to get a blank entry.
#
# Returns a dictionary containing all the details of the application. There are
# two major kinds of information in the dictionary. Keys beginning with capital
# letters correspond directory to identically named keys in the metadata file.
# Keys beginning with lower case letters are generated in one way or another,
# and are not found verbatim in the metadata.
#
# Known keys not originating from the metadata are:
#
#  'id'               - the application's package ID
#  'builds'           - a list of dictionaries containing build information
#                       for each defined build
#  'comments'         - a list of comments from the metadata file. Each is
#                       a tuple of the form (field, comment) where field is
#                       the name of the field it preceded in the metadata
#                       file. Where field is None, the comment goes at the
#                       end of the file. Alternatively, 'build:version' is
#                       for a comment before a particular build version.
#  'descriptionlines' - original lines of description as formatted in the
#                       metadata file.
#
def parse_metadata(metafile, **kw):

    def parse_buildline(lines):
        value = "".join(lines)
        parts = [p.replace("\\,", ",")
                 for p in re.split(r"(?<!\\),", value)]
        if len(parts) < 3:
            raise MetaDataException("Invalid build format: " + value + " in " + metafile.name)
        thisbuild = {}
        thisbuild['origlines'] = lines
        thisbuild['version'] = parts[0]
        thisbuild['vercode'] = parts[1]
        try:
            testvercode = int(thisbuild['vercode'])
        except:
            raise MetaDataException("Invalid version code for build in " + metafile.name)
        thisbuild['commit'] = parts[2]
        for p in parts[3:]:
            pk, pv = p.split('=', 1)
            thisbuild[pk.strip()] = pv
        return thisbuild

    def add_comments(key):
        for comment in curcomments:
            thisinfo['comments'].append((key, comment))
        del curcomments[:]

    thisinfo = {}
    if metafile:
        if not isinstance(metafile, file):
            metafile = open(metafile, "r")
        thisinfo['id'] = metafile.name[9:-4]
    else:
        thisinfo['id'] = None

    # Defaults for fields that come from metadata...
    thisinfo['Name'] = None
    thisinfo['Auto Name'] = ''
    thisinfo['Category'] = 'None'
    thisinfo['Description'] = []
    thisinfo['Summary'] = ''
    thisinfo['License'] = 'Unknown'
    thisinfo['Web Site'] = ''
    thisinfo['Source Code'] = ''
    thisinfo['Issue Tracker'] = ''
    thisinfo['Donate'] = None
    thisinfo['FlattrID'] = None
    thisinfo['Bitcoin'] = None
    thisinfo['Litecoin'] = None
    thisinfo['Disabled'] = None
    thisinfo['AntiFeatures'] = None
    thisinfo['Archive Policy'] = None
    thisinfo['Update Check Mode'] = 'None'
    thisinfo['Auto Update Mode'] = 'None'
    thisinfo['Current Version'] = ''
    thisinfo['Current Version Code'] = '0'
    thisinfo['Repo Type'] = ''
    thisinfo['Repo'] = ''
    thisinfo['Requires Root'] = False
    thisinfo['No Source Since'] = ''

    # General defaults...
    thisinfo['builds'] = []
    thisinfo['comments'] = []

    if metafile is None:
        return thisinfo

    mode = 0
    buildlines = []
    curcomments = []

    for line in metafile:
        line = line.rstrip('\r\n')
        if mode == 0:
            if len(line) == 0:
                continue
            if line.startswith("#"):
                curcomments.append(line)
                continue
            index = line.find(':')
            if index == -1:
                raise MetaDataException("Invalid metadata in " + metafile.name + " at: " + line)
            field = line[:index]
            value = line[index+1:]

            # Translate obsolete fields...
            if field == 'Market Version':
                field = 'Current Version'
            if field == 'Market Version Code':
                field = 'Current Version Code'

            fieldtype = metafieldtype(field)
            if fieldtype != 'build':
                add_comments(field)
            if fieldtype == 'multiline':
                mode = 1
                thisinfo[field] = []
                if len(value) > 0:
                    raise MetaDataException("Unexpected text on same line as " + field + " in " + metafile.name)
            elif fieldtype == 'string':
                thisinfo[field] = value
            elif fieldtype == 'flag':
                if value == 'Yes':
                    thisinfo[field] = True
                elif value == 'No':
                    thisinfo[field] = False
                else:
                    raise MetaDataException("Expected Yes or No for " + field + " in " + metafile.name)
            elif fieldtype == 'build':
                if value.endswith("\\"):
                    mode = 2
                    buildlines = [value[:-1]]
                else:
                    thisinfo['builds'].append(parse_buildline([value]))
                    add_comments('build:' + thisinfo['builds'][-1]['version'])
            elif fieldtype == 'obsolete':
                pass        # Just throw it away!
            else:
                raise MetaDataException("Unrecognised field type for " + field + " in " + metafile.name)
        elif mode == 1:     # Multiline field
            if line == '.':
                mode = 0
            else:
                thisinfo[field].append(line)
        elif mode == 2:     # Line continuation mode in Build Version
            if line.endswith("\\"):
                buildlines.append(line[:-1])
            else:
                buildlines.append(line)
                thisinfo['builds'].append(
                    parse_buildline(buildlines))
                add_comments('build:' + thisinfo['builds'][-1]['version'])
                mode = 0
    add_comments(None)

    # Mode at end of file should always be 0...
    if mode == 1:
        raise MetaDataException(field + " not terminated in " + metafile.name)
    elif mode == 2:
        raise MetaDataException("Unterminated continuation in " + metafile.name)

    if len(thisinfo['Description']) == 0:
        thisinfo['Description'].append('No description available')

    # Validate archive policy...
    if thisinfo['Archive Policy']:
        if not thisinfo['Archive Policy'].endswith(' versions'):
            raise MetaDataException("Invalid archive policy")
        try:
            versions = int(thisinfo['Archive Policy'][:-9])
            if versions < 1 or versions > 20:
                raise MetaDataException("Silly number of versions for archive policy")
        except:
            raise MetaDataException("Incomprehensible number of versions for archive policy")

    # Ensure all AntiFeatures are recognised...
    if thisinfo['AntiFeatures']:
        parts = thisinfo['AntiFeatures'].split(",")
        for part in parts:
            if (part != "Ads" and
                part != "Tracking" and
                part != "NonFreeNet" and
                part != "NonFreeDep" and
                part != "NonFreeAdd"):
                raise MetaDataException("Unrecognised antifeature '" + part + "' in " \
                            + metafile.name)

    return thisinfo

def getvercode(build):
    return "%s" % (build['vercode'])

def getapkname(app, build):
    return "%s_%s.apk" % (app['id'], getvercode(build))

def getsrcname(app, build):
    return "%s_%s_src.tar.gz" % (app['id'], getvercode(build))

# Write a metadata file.
#
# 'dest'    - The path to the output file
# 'app'     - The app data
def write_metadata(dest, app):

    def writecomments(key):
        for pf, comment in app['comments']:
            if pf == key:
                mf.write(comment + '\n')

    def writefield(field, value=None):
        writecomments(field)
        if value is None:
            value = app[field]
        mf.write(field + ':' + value + '\n')

    mf = open(dest, 'w')
    if app['Disabled']:
        writefield('Disabled')
    if app['AntiFeatures']:
        writefield('AntiFeatures')
    writefield('Category')
    writefield('License')
    writefield('Web Site')
    writefield('Source Code')
    writefield('Issue Tracker')
    if app['Donate']:
        writefield('Donate')
    if app['FlattrID']:
        writefield('FlattrID')
    if app['Bitcoin']:
        writefield('Bitcoin')
    if app['Litecoin']:
        writefield('Litecoin')
    mf.write('\n')
    if app['Name']:
        writefield('Name')
    writefield('Auto Name')
    writefield('Summary')
    writefield('Description', '')
    for line in app['Description']:
        mf.write(line + '\n')
    mf.write('.\n')
    mf.write('\n')
    if app['Requires Root']:
        writefield('Requires Root', 'Yes')
        mf.write('\n')
    if len(app['Repo Type']) > 0:
        writefield('Repo Type')
        writefield('Repo')
        mf.write('\n')
    keystoignore = ['version', 'vercode', 'subvercode', 'commit']
    for build in app['builds']:
        writecomments('build:' + build['version'])
        mf.write('Build Version:')
        if 'origlines' in build:
            # Keeping the original formatting if we loaded it from a file...
            mf.write('\\\n'.join(build['origlines']) + '\n')
        else:
            mf.write("%s,%s,%s" % (
                build['version'],
                getvercode(build),
                build['commit']))
            for key,value in build.iteritems():
                if key not in keystoignore:
                    mf.write(',' + key + '=' + value)
            mf.write('\n')
    if len(app['builds']) > 0:
        mf.write('\n')
    if app['Archive Policy']:
        writefield('Archive Policy')
    writefield('Auto Update Mode')
    writefield('Update Check Mode')
    if 'Update Check Data' in app:
        writefield('Update Check Data')
    if len(app['Current Version']) > 0:
        writefield('Current Version')
        writefield('Current Version Code')
    mf.write('\n')
    if len(app['No Source Since']) > 0:
        writefield('No Source Since')
        mf.write('\n')
    writecomments(None)
    mf.close()


# Read all metadata. Returns a list of 'app' objects (which are dictionaries as
# returned by the parse_metadata function.
def read_metadata(verbose=False, xref=True):
    apps = []
    for metafile in sorted(glob.glob(os.path.join('metadata', '*.txt'))):
        try:
            appinfo = parse_metadata(metafile, verbose=verbose)
        except Exception, e:
            raise MetaDataException("Problem reading metadata file %s: - %s" % (metafile, str(e)))
        apps.append(appinfo)

    if xref:
        # Parse all descriptions at load time, just to ensure cross-referencing
        # errors are caught early rather than when they hit the build server.
        def linkres(link):
            for app in apps:
                if app['id'] == link:
                    return ("fdroid.app:" + link, "Dummy name - don't know yet")
            raise MetaDataException("Cannot resolve app id " + link)
        for app in apps:
            try:
                description_html(app['Description'], linkres)
            except Exception, e:
                raise MetaDataException("Problem with description of " + app['id'] +
                        " - " + str(e))

    return apps

# Formatter for descriptions. Create an instance, and call parseline() with
# each line of the description source from the metadata. At the end, call
# end() and then text_plain, text_wiki and text_html will contain the result.
class DescriptionFormatter:
    stNONE = 0
    stPARA = 1
    stUL = 2
    stOL = 3
    bold = False
    ital = False
    state = stNONE
    text_plain = ''
    text_wiki = ''
    text_html = ''
    linkResolver = None
    def __init__(self, linkres):
        self.linkResolver = linkres
    def endcur(self, notstates=None):
        if notstates and self.state in notstates:
            return
        if self.state == self.stPARA:
            self.endpara()
        elif self.state == self.stUL:
            self.endul()
        elif self.state == self.stOL:
            self.endol()
    def endpara(self):
        self.text_plain += '\n'
        self.text_html += '</p>'
        self.state = self.stNONE
    def endul(self):
        self.text_html += '</ul>'
        self.state = self.stNONE
    def endol(self):
        self.text_html += '</ol>'
        self.state = self.stNONE

    def formatted(self, txt, html):
        formatted = ''
        if html:
            txt = cgi.escape(txt)
        while True:
            index = txt.find("''")
            if index == -1:
                return formatted + txt
            formatted += txt[:index]
            txt = txt[index:]
            if txt.startswith("'''"):
                if html:
                    if self.bold:
                        formatted += '</b>'
                    else:
                        formatted += '<b>'
                self.bold = not self.bold
                txt = txt[3:]
            else:
                if html:
                    if self.ital:
                        formatted += '</i>'
                    else:
                        formatted += '<i>'
                self.ital = not self.ital
                txt = txt[2:]


    def linkify(self, txt):
        linkified_plain = ''
        linkified_html = ''
        while True:
            index = txt.find("[")
            if index == -1:
                return (linkified_plain + self.formatted(txt, False), linkified_html + self.formatted(txt, True))
            linkified_plain += self.formatted(txt[:index], False)
            linkified_html += self.formatted(txt[:index], True)
            txt = txt[index:]
            if txt.startswith("[["):
                index = txt.find("]]")
                if index == -1:
                    raise MetaDataException("Unterminated ]]")
                url = txt[2:index]
                if self.linkResolver:
                    url, urltext = self.linkResolver(url)
                else:
                    urltext = url
                linkified_html += '<a href="' + url + '">' + cgi.escape(urltext) + '</a>'
                linkified_plain += urltext
                txt = txt[index+2:]
            else:
                index = txt.find("]")
                if index == -1:
                    raise MetaDataException("Unterminated ]")
                url = txt[1:index]
                index2 = url.find(' ')
                if index2 == -1:
                    urltxt = url
                else:
                    urltxt = url[index2 + 1:]
                    url = url[:index2]
                linkified_html += '<a href="' + url + '">' + cgi.escape(urltxt) + '</a>'
                linkified_plain += urltxt
                if urltxt != url:
                    linkified_plain += ' (' + url + ')'
                txt = txt[index+1:]

    def addtext(self, txt):
        p, h = self.linkify(txt)
        self.text_plain += p
        self.text_html += h

    def parseline(self, line):
        self.text_wiki += line + '\n'
        if len(line) == 0:
            self.endcur()
        elif line.startswith('*'):
            self.endcur([self.stUL])
            if self.state != self.stUL:
                self.text_html += '<ul>'
                self.state = self.stUL
            self.text_html += '<li>'
            self.text_plain += '*'
            self.addtext(line[1:])
            self.text_html += '</li>'
        elif line.startswith('#'):
            self.endcur([self.stOL])
            if self.state != self.stOL:
                self.text_html += '<ol>'
                self.state = self.stOL
            self.text_html += '<li>'
            self.text_plain += '*' #TODO: lazy - put the numbers in!
            self.addtext(line[1:])
            self.text_html += '</li>'
        else:
            self.endcur([self.stPARA])
            if self.state == self.stNONE:
                self.text_html += '<p>'
                self.state = self.stPARA
            elif self.state == self.stPARA:
                self.text_html += ' '
                self.text_plain += ' '
            self.addtext(line)

    def end(self):
        self.endcur()

# Parse multiple lines of description as written in a metadata file, returning
# a single string in plain text format.
def description_plain(lines, linkres):
    ps = DescriptionFormatter(linkres)
    for line in lines:
        ps.parseline(line)
    ps.end()
    return ps.text_plain

# Parse multiple lines of description as written in a metadata file, returning
# a single string in wiki format.
def description_wiki(lines):
    ps = DescriptionFormatter(None)
    for line in lines:
        ps.parseline(line)
    ps.end()
    return ps.text_wiki

# Parse multiple lines of description as written in a metadata file, returning
# a single string in HTML format.
def description_html(lines,linkres):
    ps = DescriptionFormatter(linkres)
    for line in lines:
        ps.parseline(line)
    ps.end()
    return ps.text_html

def retrieve_string(xml_dir, string):
    if not string.startswith('@string/'):
        return string.replace("\\'","'")
    string_search = re.compile(r'.*"'+string[8:]+'".*>([^<]+?)<.*').search
    for xmlfile in glob.glob(os.path.join(xml_dir, '*.xml')):
        for line in file(xmlfile):
            matches = string_search(line)
            if matches:
                return retrieve_string(xml_dir, matches.group(1))
    return ''

# Return list of existing files that will be used to find the highest vercode
def manifest_paths(app_dir, flavour):

    possible_manifests = [ os.path.join(app_dir, 'AndroidManifest.xml'),
            os.path.join(app_dir, 'src', 'main', 'AndroidManifest.xml'),
            os.path.join(app_dir, 'build.gradle') ]

    if flavour is not None:
        possible_manifests.append(
                os.path.join(app_dir, 'src', flavour, 'AndroidManifest.xml'))
    
    return [path for path in possible_manifests if os.path.isfile(path)]

# Retrieve the package name
def fetch_real_name(app_dir, flavour):
    app_search = re.compile(r'.*<application.*').search
    name_search = re.compile(r'.*android:label="([^"]+)".*').search
    app_found = False
    name = None
    for f in manifest_paths(app_dir, flavour):
        if not f.endswith(".xml"):
            continue
        xml_dir = os.path.join(f[:-19], 'res', 'values')
        for line in file(f):
            if not app_found:
                if app_search(line):
                    app_found = True
            if app_found:
                matches = name_search(line)
                if matches:
                    return retrieve_string(xml_dir, matches.group(1))
    return ''

# Retrieve the version name
def version_name(original, app_dir, flavour):
    for f in manifest_paths(app_dir, flavour):
        if not f.endswith(".xml"):
            continue
        xml_dir = os.path.join(f[:-19], 'res', 'values')
        string = retrieve_string(xml_dir, original)
        if len(string) > 0:
            return string
    return original

# Extract some information from the AndroidManifest.xml at the given path.
# Returns (version, vercode, package), any or all of which might be None.
# All values returned are strings.
def parse_androidmanifests(paths):

    if not paths:
        return (None, None, None)

    vcsearch = re.compile(r'.*android:versionCode="([0-9]+?)".*').search
    vnsearch = re.compile(r'.*android:versionName="([^"]+?)".*').search
    psearch = re.compile(r'.*package="([^"]+)".*').search

    vcsearch_g = re.compile(r'.*versionCode[ =]*([0-9]+?).*').search
    vnsearch_g = re.compile(r'.*versionName[ =]*"([^"]+?)".*').search
    psearch_g = re.compile(r'.*packageName[ =]*"([^"]+)".*').search

    max_version = None
    max_vercode = None
    max_package = None

    for path in paths:

        gradle = path.endswith("gradle")
        version = None
        vercode = None
        # Remember package name, may be defined separately from version+vercode
        package = max_package

        for line in file(path):
            if not package:
                if gradle:
                    matches = psearch_g(line)
                else:
                    matches = psearch(line)
                if matches:
                    package = matches.group(1)
            if not version:
                if gradle:
                    matches = vnsearch_g(line)
                else:
                    matches = vnsearch(line)
                if matches:
                    version = matches.group(1)
            if not vercode:
                if gradle:
                    matches = vcsearch_g(line)
                else:
                    matches = vcsearch(line)
                if matches:
                    vercode = matches.group(1)

        # Better some package name than nothing
        if max_package is None:
            max_package = package

        if max_vercode is None or (vercode is not None and vercode > max_vercode):
            max_version = version
            max_vercode = vercode
            max_package = package

    if max_version is None:
        max_version = "Unknown"

    return (max_version, max_vercode, max_package)

class BuildException(Exception):
    def __init__(self, value, stdout = None, stderr = None):
        self.value = value
        self.stdout = stdout
        self.stderr = stderr

    def get_wikitext(self):
        ret = repr(self.value) + "\n"
        if self.stdout:
            ret += "=stdout=\n"
            ret += "<pre>\n"
            ret += str(self.stdout)
            ret += "</pre>\n"
        if self.stderr:
            ret += "=stderr=\n"
            ret += "<pre>\n"
            ret += str(self.stderr)
            ret += "</pre>\n"
        return ret

    def __str__(self):
        ret = repr(self.value)
        if self.stdout:
            ret += "\n==== stdout begin ====\n%s\n==== stdout end ====" % self.stdout.strip()
        if self.stderr:
            ret += "\n==== stderr begin ====\n%s\n==== stderr end ====" % self.stderr.strip()
        return ret

class VCSException(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)

class MetaDataException(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)

def parse_srclib(metafile, **kw):

    thisinfo = {}
    if metafile and not isinstance(metafile, file):
        metafile = open(metafile, "r")

    # Defaults for fields that come from metadata
    thisinfo['Repo Type'] = ''
    thisinfo['Repo'] = ''
    thisinfo['Subdir'] = None
    thisinfo['Prepare'] = None
    thisinfo['Update Project'] = None

    if metafile is None:
        return thisinfo

    mode = 0
    buildlines = []

    for line in metafile:
        line = line.rstrip('\r\n')
        if len(line) == 0:
            continue
        if line.startswith("#"):
            continue
        index = line.find(':')
        if index == -1:
            raise MetaDataException("Invalid metadata in " + metafile.name + " at: " + line)
        field = line[:index]
        value = line[index+1:]

        if field == "Subdir":
            thisinfo[field] = value.split(',')
        else:
            thisinfo[field] = value

    return thisinfo

# Get the specified source library.
# Returns the path to it. Normally this is the path to be used when referencing
# it, which may be a subdirectory of the actual project. If you want the base
# directory of the project, pass 'basepath=True'.
def getsrclib(spec, srclib_dir, sdk_path, ndk_path="", mvn3="", basepath=False, raw=False, prepare=True, preponly=False):

    if raw:
        name = spec
        ref = None
    else:
        name, ref = spec.split('@')

    srclib_path = os.path.join('srclibs', name + ".txt")

    if not os.path.exists(srclib_path):
        raise BuildException('srclib ' + name + ' not found.')

    srclib = parse_srclib(srclib_path)

    sdir = os.path.join(srclib_dir, name)

    if not preponly:
        vcs = getvcs(srclib["Repo Type"], srclib["Repo"], sdir, sdk_path)
        vcs.srclib = (name, sdir)
        if ref:
            vcs.gotorevision(ref)

        if raw:
            return vcs

    libdir = None

    if srclib["Subdir"] is not None:
        for subdir in srclib["Subdir"]:
            libdir_candidate = os.path.join(sdir, subdir)
            if os.path.exists(libdir_candidate):
                libdir = libdir_candidate
                break

    if libdir is None:
        libdir = sdir

    if prepare:

        if srclib["Prepare"] is not None:
            cmd = srclib["Prepare"].replace('$$SDK$$', sdk_path)
            cmd = cmd.replace('$$NDK$$', ndk_path).replace('$$MVN$$', mvn3)

            p = FDroidPopen(['bash', '-x', '-c', cmd], cwd=libdir)
            if p.returncode != 0:
                raise BuildException("Error running prepare command for srclib %s"
                        % name, p.stdout, p.stderr)
        
        if srclib["Update Project"] == "Yes":
            print "Updating srclib %s at path %s" % (name, libdir)
            if subprocess.call([os.path.join(sdk_path, 'tools', 'android'),
                'update', 'project', '-p', libdir]) != 0:
                    raise BuildException( 'Error updating ' + name + ' project')

    if basepath:
        return sdir
    return libdir


# Prepare the source code for a particular build
#  'vcs'         - the appropriate vcs object for the application
#  'app'         - the application details from the metadata
#  'build'       - the build details from the metadata
#  'build_dir'   - the path to the build directory, usually
#                   'build/app.id'
#  'srclib_dir'  - the path to the source libraries directory, usually
#                   'build/srclib'
#  'extlib_dir'  - the path to the external libraries directory, usually
#                   'build/extlib'
#  'sdk_path'    - the path to the Android SDK
#  'ndk_path'    - the path to the Android NDK
#  'javacc_path' - the path to javacc
#  'mvn3'        - the path to the maven 3 executable
#  'verbose'     - optional: verbose or not (default=False)
# Returns the (root, srclibpaths) where:
#   'root' is the root directory, which may be the same as 'build_dir' or may
#          be a subdirectory of it.
#   'srclibpaths' is information on the srclibs being used
def prepare_source(vcs, app, build, build_dir, srclib_dir, extlib_dir, sdk_path, ndk_path, javacc_path, mvn3, verbose=False, onserver=False):

    # Optionally, the actual app source can be in a subdirectory...
    if 'subdir' in build:
        root_dir = os.path.join(build_dir, build['subdir'])
    else:
        root_dir = build_dir

    # Get a working copy of the right revision...
    print "Getting source for revision " + build['commit']
    vcs.gotorevision(build['commit'])

    # Check that a subdir (if we're using one) exists. This has to happen
    # after the checkout, since it might not exist elsewhere...
    if not os.path.exists(root_dir):
        raise BuildException('Missing subdir ' + root_dir)

    # Initialise submodules if requred...
    if build.get('submodules', 'no')  == 'yes':
        if verbose: print "Initialising submodules..."
        vcs.initsubmodules()

    # Run an init command if one is required...
    if 'init' in build:
        cmd = build['init']
        cmd = cmd.replace('$$SDK$$', sdk_path)
        cmd = cmd.replace('$$NDK$$', ndk_path)
        cmd = cmd.replace('$$MVN$$', mvn3)
        if verbose:
            print "Running 'init' commands in %s" % root_dir

        p = FDroidPopen(['bash', '-x', '-c', cmd], cwd=root_dir, verbose=verbose)
        if p.returncode != 0:
            raise BuildException("Error running init command for %s:%s" %
                    (app['id'], build['version']), p.stdout, p.stderr)

    # Generate (or update) the ant build file, build.xml...
    updatemode = build.get('update', '.')
    if (updatemode != 'no' and
        'maven' not in build and 'gradle' not in build):
        parms = [os.path.join(sdk_path, 'tools', 'android'),
                'update', 'project', '-p', '.']
        parms.append('--subprojects')
        if 'target' in build:
            parms.append('-t')
            parms.append(build['target'])
        update_dirs = [d.strip() for d in updatemode.split(';')]
        # Force build.xml update if necessary...
        if updatemode == 'force' or 'target' in build:
            if updatemode == 'force':
                update_dirs = ['.']
            buildxml = os.path.join(root_dir, 'build.xml')
            if os.path.exists(buildxml):
                print 'Force-removing old build.xml'
                os.remove(buildxml)

        baddirs = [
                'gen', 'bin', 'obj', # ant
                'libs/armeabi-v7a', 'libs/armeabi', # jni
                'libs/mips', 'libs/x86'] # jni
        if 'gradle' in build:
            baddirs.append('build')
        if 'maven' in build:
            baddirs.append('target')
        for baddir in baddirs:
            badpath = os.path.join(build_dir, baddir)
            if os.path.exists(badpath):
                print "Removing '%s'" % badpath
                shutil.rmtree(badpath)
        for d in update_dirs:
            cwd = os.path.join(root_dir, d)
            # Remove gen and bin dirs in libraries
            # rid of them...
            for baddir in [
                    'gen', 'bin', 'obj', # ant
                    'libs/armeabi-v7a', 'libs/armeabi', # jni
                    'libs/mips', 'libs/x86']:
                badpath = os.path.join(cwd, baddir)
                if os.path.exists(badpath):
                    print "Removing '%s'" % badpath
                    shutil.rmtree(badpath)
            if verbose:
                print "Update of '%s': exec '%s' in '%s'"%\
                    (d," ".join(parms),cwd)
            p = FDroidPopen(parms, cwd=cwd, verbose=verbose)
            # check to see whether an error was returned without a proper exit code (this is the case for the 'no target set or target invalid' error)
            if p.returncode != 0 or (p.stderr != "" and p.stderr.startswith("Error: ")):
                raise BuildException("Failed to update project at %s" % cwd,
                        p.stdout, p.stderr)

    # If the app has ant set up to sign the release, we need to switch
    # that off, because we want the unsigned apk...
    for propfile in ('build.properties', 'default.properties', 'ant.properties'):
        if os.path.exists(os.path.join(root_dir, propfile)):
            if subprocess.call(['sed','-i','s/^key.store/#/',
                                propfile], cwd=root_dir) !=0:
                raise BuildException("Failed to amend %s" % propfile)

    # Update the local.properties file...
    locprops = os.path.join(root_dir, 'local.properties')
    if os.path.exists(locprops):
        f = open(locprops, 'r')
        props = f.read()
        f.close()
        # Fix old-fashioned 'sdk-location' by copying
        # from sdk.dir, if necessary...
        if build.get('oldsdkloc', 'no') == "yes":
            sdkloc = re.match(r".*^sdk.dir=(\S+)$.*", props,
                re.S|re.M).group(1)
            props += "\nsdk-location=" + sdkloc + "\n"
        # Add ndk location...
        props+= "\nndk.dir=" + ndk_path + "\n"
        # Add java.encoding if necessary...
        if 'encoding' in build:
            props += "\njava.encoding=" + build['encoding'] + "\n"
        f = open(locprops, 'w')
        f.write(props)
        f.close()

    # Insert version code and number into the manifest if necessary...
    flavour = None
    if 'gradle' in build:
        flavour = build['gradle'].split('@')[0]
        if flavour in ['main', 'yes', '']:
            flavour = None
    if 'forceversion' in build:
        for path in manifest_paths(root_dir, flavour):
            if not os.path.isfile(path):
                continue
            if subprocess.call(['sed','-r','-i',
                's/android:versionName="[^"]+"/android:versionName="' + build['version'] + '"/g',
                path]) != 0:
                raise BuildException("Failed to amend manifest")
    if 'forcevercode' in build:
        for path in manifest_paths(root_dir, flavour):
            if not os.path.isfile(path):
                continue
            if subprocess.call(['sed','-r','-i',
                's/android:versionCode="[^"]+"/android:versionCode="' + build['vercode'] + '"/g',
                path]) != 0:
                raise BuildException("Failed to amend manifest")

    # Delete unwanted file...
    if 'rm' in build:
        for part in build['rm'].split(';'):
            dest = os.path.join(build_dir, part.strip())
            if os.path.exists(dest):
                os.remove(dest)

    # Fix apostrophes translation files if necessary...
    if build.get('fixapos', 'no') == 'yes':
        for root, dirs, files in os.walk(os.path.join(root_dir, 'res')):
            for filename in files:
                if filename.endswith('.xml'):
                    if subprocess.call(['sed','-i','s@' +
                        r"\([^\\]\)'@\1\\'" +
                        '@g',
                        os.path.join(root, filename)]) != 0:
                        raise BuildException("Failed to amend " + filename)

    # Fix translation files if necessary...
    if build.get('fixtrans', 'no') == 'yes':
        for root, dirs, files in os.walk(os.path.join(root_dir, 'res')):
            for filename in files:
                if filename.endswith('.xml'):
                    f = open(os.path.join(root, filename))
                    changed = False
                    outlines = []
                    for line in f:
                        num = 1
                        index = 0
                        oldline = line
                        while True:
                            index = line.find("%", index)
                            if index == -1:
                                break
                            next = line[index+1:index+2]
                            if next == "s" or next == "d":
                                line = (line[:index+1] +
                                        str(num) + "$" +
                                        line[index+1:])
                                num += 1
                                index += 3
                            else:
                                index += 1
                        # We only want to insert the positional arguments
                        # when there is more than one argument...
                        if oldline != line:
                            if num > 2:
                                changed = True
                            else:
                                line = oldline
                        outlines.append(line)
                    f.close()
                    if changed:
                        f = open(os.path.join(root, filename), 'w')
                        f.writelines(outlines)
                        f.close()

    # Add required external libraries...
    if 'extlibs' in build:
        print "Collecting prebuilt libraries..."
        libsdir = os.path.join(root_dir, 'libs')
        if not os.path.exists(libsdir):
            os.mkdir(libsdir)
        for lib in build['extlibs'].split(';'):
            lib = lib.strip()
            libf = os.path.basename(lib)
            shutil.copyfile(os.path.join(extlib_dir, lib),
                    os.path.join(libsdir, libf))

    # Get required source libraries...
    srclibpaths = []
    if 'srclibs' in build:
        print "Collecting source libraries..."
        for lib in build['srclibs'].split(';'):
            lib = lib.strip()
            name, _ = lib.split('@')
            srclibpaths.append((name, getsrclib(lib, srclib_dir, sdk_path, ndk_path, mvn3, preponly=onserver)))
    basesrclib = vcs.getsrclib()
    # If one was used for the main source, add that too.
    if basesrclib:
        srclibpaths.append(basesrclib)

    # Apply patches if any
    if 'patch' in build:
        for patch in build['patch'].split(';'):
            patch = patch.strip()
            print "Applying " + patch
            patch_path = os.path.join('metadata', app['id'], patch)
            if subprocess.call(['patch', '-p1',
                            '-i', os.path.abspath(patch_path)], cwd=build_dir) != 0:
                raise BuildException("Failed to apply patch %s" % patch_path)

    # Run a pre-build command if one is required...
    if 'prebuild' in build:
        output = ''
        error = ''
        cmd = build['prebuild']

        # Substitute source library paths into prebuild commands...
        for name, libpath in srclibpaths:
            libpath = os.path.relpath(libpath, root_dir)
            cmd = cmd.replace('$$' + name + '$$', libpath)
        cmd = cmd.replace('$$SDK$$', sdk_path)
        cmd = cmd.replace('$$NDK$$', ndk_path)
        cmd = cmd.replace('$$MVN3$$', mvn3)
        if verbose:
            print "Running 'prebuild' commands in %s" % root_dir

        p = FDroidPopen(['bash', '-x', '-c', cmd], cwd=root_dir, verbose=verbose)
        if p.returncode != 0:
            raise BuildException("Error running prebuild command for %s:%s" %
                    (app['id'], build['version']), p.stdout, p.stderr)
    print "Applying generic clean-ups..."

    if build.get('anal-tics', 'no') == 'yes':
        fp = os.path.join(root_dir, 'src', 'com', 'google', 'android', 'apps', 'analytics')
        os.makedirs(fp)
        with open(os.path.join(fp, 'GoogleAnalyticsTracker.java'), 'w') as f:
            f.write("""
            package com.google.android.apps.analytics;
            public class GoogleAnalyticsTracker {
                private static GoogleAnalyticsTracker instance;
                private GoogleAnalyticsTracker() {
                }
                public static GoogleAnalyticsTracker getInstance() {
                    if(instance == null)
                        instance = new GoogleAnalyticsTracker();
                    return instance;
                }
                public void start(String i,int think ,Object not) {
                }
                public void dispatch() {
                }
                public void stop() {
                }
                public void setProductVersion(String uh, String hu) {
                }
                public void trackEvent(String that,String just,String aint,int happening) {
                }
                public void trackPageView(String nope) {
                }
                public void setCustomVar(int mind,String your,String own,int business) {
                }
            }
            """)

    # Special case init functions for funambol...
    if build.get('initfun', 'no')  == "yes":

        if subprocess.call(['sed','-i','s@' +
            '<taskdef resource="net/sf/antcontrib/antcontrib.properties" />' +
            '@' +
            '<taskdef resource="net/sf/antcontrib/antcontrib.properties">' +
            '<classpath>' +
            '<pathelement location="/usr/share/java/ant-contrib.jar"/>' +
            '</classpath>' +
            '</taskdef>' +
            '@g',
            'build.xml'], cwd=root_dir) !=0:
            raise BuildException("Failed to amend build.xml")

        if subprocess.call(['sed','-i','s@' +
            '\${user.home}/funambol/build/android/build.properties' +
            '@' +
            'build.properties' +
            '@g',
            'build.xml'], cwd=root_dir) !=0:
            raise BuildException("Failed to amend build.xml")

        buildxml = os.path.join(root_dir, 'build.xml')
        f = open(buildxml, 'r')
        xml = f.read()
        f.close()
        xmlout = ""
        mode = 0
        for line in xml.splitlines():
            if mode == 0:
                if line.find("jarsigner") != -1:
                    mode = 1
                else:
                    xmlout += line + "\n"
            else:
                if line.find("/exec") != -1:
                    mode += 1
                    if mode == 3:
                        mode =0
        f = open(buildxml, 'w')
        f.write(xmlout)
        f.close()

        if subprocess.call(['sed','-i','s@' +
            'platforms/android-2.0' +
            '@' +
            'platforms/android-8' +
            '@g',
            'build.xml'], cwd=root_dir) !=0:
            raise BuildException("Failed to amend build.xml")

        shutil.copyfile(
                os.path.join(root_dir, "build.properties.example"),
                os.path.join(root_dir, "build.properties"))

        if subprocess.call(['sed','-i','s@' +
            'javacchome=.*'+
            '@' +
            'javacchome=' + javacc_path +
            '@g',
            'build.properties'], cwd=root_dir) !=0:
            raise BuildException("Failed to amend build.properties")

        if subprocess.call(['sed','-i','s@' +
            'sdk-folder=.*'+
            '@' +
            'sdk-folder=' + sdk_path +
            '@g',
            'build.properties'], cwd=root_dir) !=0:
            raise BuildException("Failed to amend build.properties")

        if subprocess.call(['sed','-i','s@' +
            'android.sdk.version.*'+
            '@' +
            'android.sdk.version=2.0' +
            '@g',
            'build.properties'], cwd=root_dir) !=0:
            raise BuildException("Failed to amend build.properties")

    return (root_dir, srclibpaths)


# Scan the source code in the given directory (and all subdirectories)
# and return a list of potential problems.
def scan_source(build_dir, root_dir, thisbuild):

    problems = []

    # Common known non-free blobs (always lower case):
    usual_suspects = ['flurryagent',
                      'paypal_mpl',
                      'libgoogleanalytics',
                      'admob-sdk-android',
                      'googleadview',
                      'googleadmobadssdk',
                      'google-play-services',
                      'crittercism',
                      'heyzap',
                      'jpct-ae',
                      'youtubeandroidplayerapi']

    if 'scanignore' in thisbuild:
        ignore = [p.strip() for p in thisbuild['scanignore'].split(';')]
    else:
        ignore = []
    
    ms = magic.open(magic.MIME_TYPE)
    ms.load()

    # Iterate through all files in the source code...
    for r,d,f in os.walk(build_dir):
        for curfile in f:

            if '/.hg' in r or '/.git' in r or '/.svn' in r:
                continue

            # Path (relative) to the file...
            fp = os.path.join(r, curfile)
            fd = fp[len(build_dir)+1:]

            # Check if this file has been explicitly excluded from scanning...
            ignorethis = False
            for i in ignore:
                if fd.startswith(i):
                    ignorethis = True
                    break
            if ignorethis:
                continue

            for suspect in usual_suspects:
                if suspect in curfile.lower():
                    problems.append('Found probable non-free blob ' + fp)

            mime = ms.file(fp)
            if mime == 'application/x-sharedlib':
                problems.append('Found shared library at %s' % fd)
            elif mime == 'application/x-archive':
                problems.append('Found static library at %s' % fd)
            elif mime == 'application/x-executable':
                problems.append('Found binary executable at %s' % fd)
            elif fp.endswith('.apk'):
                problems.append('Found apk archive at %s' % fd)

            elif curfile.endswith('.java'):
                for line in file(fp):
                    if 'DexClassLoader' in line:
                        problems.append('Found DexClassLoader in ' + fp)
                        break
    ms.close()

    # Presence of a jni directory without buildjni=yes might
    # indicate a problem... (if it's not a problem, explicitly use
    # buildjni=no to bypass this check)
    if (os.path.exists(os.path.join(root_dir, 'jni')) and 
            thisbuild.get('buildjni') is None):
        msg = 'Found jni directory, but buildjni is not enabled'
        problems.append(msg)

    return problems


class KnownApks:

    def __init__(self):
        self.path = os.path.join('stats', 'known_apks.txt')
        self.apks = {}
        if os.path.exists(self.path):
            for line in file( self.path):
                t = line.rstrip().split(' ')
                if len(t) == 2:
                    self.apks[t[0]] = (t[1], None)
                else:
                    self.apks[t[0]] = (t[1], time.strptime(t[2], '%Y-%m-%d'))
        self.changed = False

    def writeifchanged(self):
        if self.changed:
            if not os.path.exists('stats'):
                os.mkdir('stats')
            f = open(self.path, 'w')
            lst = []
            for apk, app in self.apks.iteritems():
                appid, added = app
                line = apk + ' ' + appid
                if added:
                    line += ' ' + time.strftime('%Y-%m-%d', added)
                lst.append(line)
            for line in sorted(lst):
                f.write(line + '\n')
            f.close()

    # Record an apk (if it's new, otherwise does nothing)
    # Returns the date it was added.
    def recordapk(self, apk, app):
        if not apk in self.apks:
            self.apks[apk] = (app, time.gmtime(time.time()))
            self.changed = True
        _, added = self.apks[apk]
        return added

    # Look up information - given the 'apkname', returns (app id, date added/None).
    # Or returns None for an unknown apk.
    def getapp(self, apkname):
        if apkname in self.apks:
            return self.apks[apkname]
        return None

    # Get the most recent 'num' apps added to the repo, as a list of package ids
    # with the most recent first.
    def getlatest(self, num):
        apps = {}
        for apk, app in self.apks.iteritems():
            appid, added = app
            if added:
                if appid in apps:
                    if apps[appid] > added:
                        apps[appid] = added
                else:
                    apps[appid] = added
        sortedapps = sorted(apps.iteritems(), key=operator.itemgetter(1))[-num:]
        lst = []
        for app, added in sortedapps:
            lst.append(app)
        lst.reverse()
        return lst

def isApkDebuggable(apkfile):
    """Returns True if the given apk file is debuggable

    :param apkfile: full path to the apk to check"""

    execfile('config.py', globals())

    p = subprocess.Popen([os.path.join(sdk_path, 'build-tools', build_tools, 'aapt'),
          'dump', 'xmltree', apkfile, 'AndroidManifest.xml'],
         stdout=subprocess.PIPE)
    output = p.communicate()[0]
    if p.returncode != 0:
        print "ERROR: Failed to get apk manifest information"
        sys.exit(1)
    for line in output.splitlines():
        if line.find('android:debuggable') != -1 and not line.endswith('0x0'):
            return True
    return False


class AsynchronousFileReader(threading.Thread):
    '''
    Helper class to implement asynchronous reading of a file
    in a separate thread. Pushes read lines on a queue to
    be consumed in another thread.
    '''
 
    def __init__(self, fd, queue):
        assert isinstance(queue, Queue.Queue)
        assert callable(fd.readline)
        threading.Thread.__init__(self)
        self._fd = fd
        self._queue = queue
 
    def run(self):
        '''The body of the tread: read lines and put them on the queue.'''
        for line in iter(self._fd.readline, ''):
            self._queue.put(line)
 
    def eof(self):
        '''Check whether there is no more content to expect.'''
        return not self.is_alive() and self._queue.empty()

class PopenResult:
    returncode = None
    stdout = ''
    stderr = ''
    stdout_apk = ''

def FDroidPopen(commands, cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        verbose=False, apkoutput=False):
    """
    Runs a command the FDroid way and returns return code and output

    :param commands, cwd, stdout, stderr: like subprocess.Popen
    :param verbose: whether to print output as it is saved
    """
    result = PopenResult()
    p = subprocess.Popen(commands, cwd=cwd, stdout=stdout, stderr=stderr)
    
    stdout_queue = Queue.Queue()
    stdout_reader = AsynchronousFileReader(p.stdout, stdout_queue)
    stdout_reader.start()
    stderr_queue = Queue.Queue()
    stderr_reader = AsynchronousFileReader(p.stderr, stderr_queue)
    stderr_reader.start()
    
    # Check the queues for output (until there is no more to get)
    while not stdout_reader.eof() or not stderr_reader.eof():
        # Show what we received from standard output
        while not stdout_queue.empty():
            line = stdout_queue.get()
            if verbose:
                # Output directly to console
                sys.stdout.write(line)
                sys.stdout.flush()
            if apkoutput and 'apk' in line:
                result.stdout_apk += line
            result.stdout += line

        # Show what we received from standard error
        while not stderr_queue.empty():
            line = stderr_queue.get()
            if verbose:
                # Output directly to console
                sys.stderr.write(line)
                sys.stderr.flush()
            result.stderr += line
        time.sleep(0.5)

    if verbose:
        print "Directory: %s" % cwd
        print " > %s" % ' '.join(commands)
    p.communicate()
    result.returncode = p.returncode
    return result
