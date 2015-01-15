'''
Optimize a static website for hosting in S3, by including a
fingerprint into all assets\' filenames. The optimized website
is uploaded into the specified S3 bucket with the right cache
headers.
'''

from __future__ import print_function
import sys
import argparse
import os.path
import logging
import fileinput
from boto import connect_s3
from boto.s3.key import Key
from boto.exception import BotoClientError, BotoServerError
from pkg_resources import Requirement, resource_filename, require
from hashlib import sha256
from shutil import copyfile
from fnmatch import fnmatch
from tempfile import mkdtemp

__author__ = "Ruben Van den Bossche"
__email__ = "ruben@appstrakt.com"
__copyright__ = "Copyright 2015, Appstrakt BVBA"
__license__ = "MIT"
__version__ = "0.1"

logger = None

def hashfile(afile, hasher, blocksize=65536):
    '''
    Calculate a hash from a file object.
    '''

    buf = afile.read(blocksize)
    while len(buf) > 0:
        hasher.update(buf)
        buf = afile.read(blocksize)
    return hasher.hexdigest()

def convert_filename(filename, filehash):
    '''
    Convert filename to include file hash
    '''
    ftup = os.path.splitext(filename)

    return ftup[0] + '.' + filehash + ftup[1]


class OptimizerError(Exception):
    '''
    Error thrown by Optimizer class.
    '''

    def __init__(self, msg):
        self.msg = msg
    def __str__(self):
        return str(self.msg)


class Optimizer(object):
    '''
    Optimizer class: Optimize a static website for hosting in S3.
    '''

    def __init__(self, source_dir, destination_bucket, exclude=[], output_dir=None, aws_access_key_id=None, aws_secret_access_key=None):
        '''
        Initialize Optimizer
        '''

        logger.debug('Initialize Optimizer class')

        if not os.path.isdir(source_dir):
            raise OptimizerError("{0} is not a valid path".format(source_dir))

        if not os.access(source_dir, os.R_OK):
            raise OptimizerError("{0} is not a readable dir".format(source_dir))

        if output_dir != None:
            if not os.path.isdir(output_dir):
                try:
                    os.makedirs(output_dir)
                except os.error as e:
                    raise OptimizerError("{0} is not a directory and cannot be created".format(output_dir))

            if not os.access(output_dir, os.W_OK):
                raise OptimizerError("{0} is not a writable dir".format(output_dir))
        else:
            output_dir = mkdtemp()

        self._assets_ext = ['.css', '.svg', '.ttf', '.woff', '.woff2', '.otf', '.eot', '.png', '.jpg', '.jpeg', '.gif', '.js']
        self._rewriteables_ext = ['.html', '.htm', '.js', '.css']

        self._source_dir = source_dir
        self._output_dir = output_dir
        self._destination_bucket = destination_bucket
        self._subdirs = []
        self._files = []
        self._assets_map = {}
        self._rewritables = []
        self._exclude = exclude
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._skip_s3_upload = skip_s3_upload

        logger.debug('Optimizer class initialized')


    def _index_source_dir(self):
        '''
        Index all files and directories under the source directory
        '''

        logger.debug('Indexing source dir')
        for dirpath, dirnames, fnames in os.walk(self._source_dir):

            for d in dirnames:
                relpath = os.path.relpath(os.path.join(dirpath, d), self._source_dir)
                if True in [fnmatch(relpath, exclude) for exclude in self._exclude]:
                    continue

                logger.debug("Found subdir {0}".format(relpath))
                self._subdirs.append(relpath)

            for f in fnames:
                relpath = os.path.relpath(os.path.join(dirpath, f), self._source_dir)
                if True in [fnmatch(relpath, exclude) for exclude in self._exclude]:
                    continue

                self._files.append(relpath)

                ext = os.path.splitext(f)[1]
                if ext in self._assets_ext:
                    logger.debug("Found asset {0}".format(f))
                    self._assets_map[relpath] = {}

                if ext in self._rewriteables_ext:
                    logger.debug("Found rewritable {0}".format(f))
                    self._rewritables.append(relpath)

        logger.debug('Finished indexing source dir')


    def _calculate_fingerprints(self):
        '''
        Calculate fingerprints of all assets in the source dir
        '''

        logger.debug('Calculating fingerprints')

        for fname in self._assets_map.keys():
            with open(os.path.join(self._source_dir, fname), 'rb') as f:
                filehash = hashfile(f, sha256())
                logger.debug("Fingerprint of {0} is {1}".format(fname, filehash))
                self._assets_map[fname]['new_filename'] = convert_filename(fname, filehash)

        logger.debug('Finished calculating fingerprints')


    def _write_dirs(self):
        '''
        Write directory structure to output folder
        '''

        logger.debug('Writing dirs')
        for reldir in self._subdirs:
            absdir = os.path.join(self._output_dir, reldir)
            if not os.path.isdir(absdir):
                logger.debug("Making dir {0}".format(absdir))
                os.makedirs(absdir)

        logger.debug('Finished writing dirs')

    def _write_files(self):
        '''
        Write files to output folder, and rewrite file names/content if necessary.
        '''

        logger.debug('Writing files')
        assets = self._assets_map.keys()

        for src_filename in self._files:
            dst_filename = src_filename

            # if file is asset
            if src_filename in assets:
                dst_filename = self._assets_map[src_filename]['new_filename']

            src = os.path.join(self._source_dir, src_filename)
            dest = os.path.join(self._output_dir, dst_filename)

            # if file is rewritable
            if src_filename in self._rewritables:
                logger.debug("Rewriting {0}".format(dest))
                # open old and new file for reading and writing
                with open(src, 'r') as f_src:
                    with open(dest, 'w') as f_dst:
                        # replace asset urls line by line
                        for line in f_src:
                            for asset in assets:
                                # TODO: won't work if using relative paths (such as ../../main.css)
                                # TODO: won't work if html contains filenames in plain text
                                # TODO: won't work if files contain absolute paths to other domains
                                line = line.replace(asset, self._assets_map[asset]['new_filename'])
                            f_dst.write(line)

            else:
                # no rewriting necessary, just copy the file
                logger.debug("Writing file to {0}".format(dest))
                copyfile(src, dest)

        logger.debug('Finished writing files')

    def _upload_to_bucket(self):
        '''
        Upload contents of output folder to S3.
        '''
        logger.debug('Uploading to bucket')

        try:
            s3 = connect_s3(aws_access_key_id=self._aws_access_key_id, aws_secret_access_key=self._aws_secret_access_key)
            bucket = s3.get_bucket(self._destination_bucket)

            to_be_deleted = [l.key for l in bucket.list()]

            for dirpath, dirnames, fnames in os.walk(self._output_dir):

                for f in fnames:
                    abspath = os.path.join(dirpath, f)
                    relpath = os.path.relpath(abspath, self._output_dir)

                    # do not delete this file
                    try:
                        to_be_deleted.remove(relpath)
                    except:
                        pass

                    # check if file should be cached / reuploaded
                    is_asset = os.path.splitext(f)[1] in self._assets_ext


                    k = bucket.get_key(relpath)
                    if k == None or not is_asset:
                        # asset doesn't exist or file is not an asset and should be uploaded anyway

                        k = Key(bucket)
                        k.key = relpath

                        headers = {}
                        if is_asset:
                            # set infinite headers
                            headers['Cache-Control'] = "public, max-age=31556926"
                        else:
                            # set no-cache headers
                            headers['Cache-Control'] = "no-cache, max-age=0"

                        logger.debug("Uploading file {0} to {1}".format(relpath, self._destination_bucket))
                        k.set_contents_from_filename(abspath, replace=True, headers=headers)

            # remove files not currently touched
            for del_file in to_be_deleted:
                logger.debug("Deleting key {0} from {1}".format(del_file, self._destination_bucket))
            bucket.delete_keys(to_be_deleted)

        except (BotoClientError, BotoServerError) as e:
            raise OptimizerError("Error uploading to S3")

        logger.debug('Finished uploading to bucket')



    def run(self):
        '''
        Main entry method for the Optimizer object.
        '''
        logger.debug('Running optimize')

        self._index_source_dir()
        self._calculate_fingerprints()
        self._write_dirs()
        self._write_files()
        if not self._skip_s3_upload:
            self._upload_to_bucket()

        logger.debug('Finished running optimize')


def main():
    '''
    Main function for the s3-site-cache-optimizer CLI.
    '''

    # init logger
    global logger
    logger = logging.getLogger('s3-site-cache-optimizer')
    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # parse arguments
    parser = argparse.ArgumentParser(prog='s3-site-cache-optimizer', description='Optimize a static website for hosting in S3, by including a fingerprint into all assets\' filenames. The optimized website is uploaded into the specified S3 bucket with the right cache headers.')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--version', action='version', version='%(prog)s ' + str(require("s3-site-cache-optimizer")[0].version))
    parser.add_argument("source_dir", help='Local directory containing a static website.')
    parser.add_argument("destination_bucket", help='Domain name of the website and S3 bucket name.')
    parser.add_argument('--exclude', nargs='+', metavar="PATTERN", default=[], help='Exclude files and directories matching these patterns.')
    parser.add_argument('-o', '--output', dest='output_dir', default=None, help='Output directory in which local files are written. When absent a temporary directory is created and used.')
    parser.add_argument('--access-key', dest="aws_access_key_id", default=None, help='AWS access key. If this field is not specified, credentials from environment or credentials files will be used.')
    parser.add_argument('--secret-key', dest="aws_secret_access_key", default=None, help='AWS access secret. If this field is not specified, credentials from environment or credentials files will be used.')
    parser.add_argument('--skip-s3-upload', dest="skip_s3_upload", action='store_true', help='Skip uploading to S3.')

    try:
        args = parser.parse_args()
    except argparse.ArgumentTypeError as e:
        logger.error(e)
        exit(1)

    # set logging level
    if args.debug:
        logger.setLevel(logging.DEBUG)

    try:
        Optimizer(args.source_dir, args.destination_bucket, exclude=args.exclude, output_dir=args.output_dir, aws_access_key_id=args.aws_access_key_id, aws_secret_access_key=args.aws_secret_access_key, skip_s3_upload=args.skip_s3_upload).run()
    except Exception as e:
        logger.error(e)
        exit(1)
