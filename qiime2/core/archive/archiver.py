# ----------------------------------------------------------------------------
# Copyright (c) 2016-2023, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import collections
import uuid as _uuid
import pathlib
import weakref
import zipfile
import importlib
import os
import io

import qiime2
import qiime2.core.cite as cite

from qiime2.core.util import md5sum_directory, from_checksum_format, is_uuid4

_VERSION_TEMPLATE = """\
QIIME 2
archive: %s
framework: %s
"""

ArchiveRecord = collections.namedtuple(
    'ArchiveRecord', ['root', 'version_fp', 'uuid', 'version',
                      'framework_version'])

ChecksumDiff = collections.namedtuple(
    'ChecksumDiff', ['added', 'removed', 'changed'])


class _Archive:
    """Abstraction layer over the archive filesystem.

    Responsible for details concerning manipulating an archive agnostic to its
    format. It is responsible for managing archive UUID, format, and framework
    versions as those are designed to be constant throughout all future
    format implementations. Breaking compatibility with that is a BIG DEAL and
    should avoided at (nearly) any cost.

    Example filesystem::

        <archive root>/
        !--- 770509e6-85f4-432c-9663-cdc04eb07db2
            |--- VERSION
            !--- <whatever format defines>

    VERSION file::

        QIIME 2
        archive: <archive version>
        framework: <framework version>

    This file is itentionally not YAML/INI/An actual format. This is to
    discourage the situation where the format changes from something like YAML
    to another format and VERSION is updated with it "for consistency".

    To emphasize, the VERSION (filepath and content) and root archive structure
    MUST NOT CHANGE. If they change, then there is no longer a consistent way
    to dispatch to an appropriate format.

    """
    VERSION_FILE = 'VERSION'

    @classmethod
    def is_archive_type(cls, filepath):
        raise NotImplementedError

    @classmethod
    def setup(cls, uuid, path, version, framework_version):
        root_dir = path
        version_fp = root_dir / cls.VERSION_FILE

        version_fp.write_text(_VERSION_TEMPLATE % (version, framework_version))

        return ArchiveRecord(root_dir, version_fp, uuid, version,
                             framework_version)

    @classmethod
    def save(cls, source, destination):
        raise NotImplementedError

    def __init__(self, path):
        self.path = path

        self.uuid = self._get_uuid()
        self.version, self.framework_version = self._get_versions()

    def _get_uuid(self):
        if not self.path.exists():
            raise TypeError("%s does not exist or is not a filepath."
                            % self.path)

        roots = set()
        for relpath in self.relative_iterdir():
            if not relpath.startswith('.'):
                roots.add(relpath)

        if len(roots) == 0:
            raise ValueError("Archive does not have a visible root directory.")
        if len(roots) > 1:
            raise ValueError("Archive has multiple root directories: %r"
                             % roots)
        uuid = roots.pop()
        if not is_uuid4(uuid):
            raise ValueError(
                "Archive root directory name %r is not a valid version 4 "
                "UUID." % uuid)
        return uuid

    def _get_versions(self):
        try:
            with self.open(self.VERSION_FILE) as fh:
                header, version_line, framework_version_line, eof = \
                    fh.read().split('\n')
            if header.strip() != 'QIIME 2':
                raise Exception()  # GOTO except Exception
            version = version_line.split(':')[1].strip()
            framework_version = framework_version_line.split(':')[1].strip()
            return version, framework_version
        except Exception:
            # TODO: make a "better" parser which isn't just a catch-all
            raise ValueError("Archive does not contain a correctly formatted"
                             " VERSION file.")

    def relative_iterdir(self, relpath='.'):
        raise NotImplementedError

    def open(self, relpath):
        raise NotImplementedError

    def mount(self, filepath):
        raise NotImplementedError


class _ZipArchive(_Archive):
    """A specific variant of Archive which deals with ZIP64 files."""

    @classmethod
    def is_archive_type(cls, path):
        return zipfile.is_zipfile(str(path))

    @classmethod
    def save(cls, source, destination):
        parent_dir = os.path.split(source)[0]
        with zipfile.ZipFile(str(destination), mode='w',
                             compression=zipfile.ZIP_DEFLATED,
                             allowZip64=True) as zf:
            for root, dirs, files in os.walk(str(source)):
                # Prune hidden directories from traversal. Strategy modified
                # from http://stackoverflow.com/a/13454267/3776794
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    if file.startswith('.'):
                        continue

                    abspath = pathlib.Path(root) / file
                    relpath = abspath.relative_to(parent_dir)

                    zf.write(str(abspath), arcname=cls._as_zip_path(relpath))

    def relative_iterdir(self, relpath=''):
        relpath = self._as_zip_path(relpath)
        seen = set()
        with zipfile.ZipFile(str(self.path), mode='r') as zf:
            for name in zf.namelist():
                if name.startswith(relpath):
                    parts = pathlib.PurePosixPath(name).parts
                    if len(parts) > 0:
                        result = parts[0]
                        if result not in seen:
                            seen.add(result)
                            yield result

    def open(self, relpath):
        relpath = pathlib.Path(str(self.uuid)) / relpath
        with zipfile.ZipFile(str(self.path), mode='r') as zf:
            # The filehandle will still work even when `zf` is "closed"
            return io.TextIOWrapper(zf.open(self._as_zip_path(relpath)))

    def mount(self, filepath):
        # TODO: use FUSE/MacFUSE/Dokany bindings (many Python bindings are
        # outdated, we may need to take up maintenance/fork)

        # We will have already allocated filepath at this point, we check if
        # the VERSION file exists to determine whether or not we have alredy
        # written to the allocated directory. This is relevant when you try to
        # load an artifact that is already in the cache because data/<uuid>
        # will be read only, so attempting to extract there will error. We also
        # just don't need to put the data there again if it is already there
        if not os.path.exists(filepath / 'VERSION'):
            self.extract(filepath)

        root = filepath
        return ArchiveRecord(root, root / self.VERSION_FILE,
                             self.uuid, self.version, self.framework_version)

    def extract(self, filepath):
        filepath = pathlib.Path(filepath)
        assert os.path.basename(filepath) == str(self.uuid)
        with zipfile.ZipFile(str(self.path), mode='r') as zf:
            for name in zf.namelist():
                if name.startswith(str(self.uuid)):
                    # extract removes `..` components, so as long as we extract
                    # into `filepath`, the path won't go backwards.
                    zf.extract(name, path=str(filepath.parent))

        return filepath

    @classmethod
    def _as_zip_path(self, path):
        path = str(pathlib.PurePosixPath(path))
        # zip files don't work well with '.' which is the identity of a Path
        # obj, so just convert to empty string which is basically the identity
        # of a zip's entry
        if path == '.':
            path = ''
        return path


class _NoOpArchive(_Archive):
    """For dealing with unzipped artifacts"""

    @classmethod
    def is_archive_type(cls, path):
        return os.path.isdir(str(path))

    def _get_uuid(self):
        """If we are using a _NoOpArchive we are a data element in a pool
        meaning we are unzipped and our name is our uuid
        """
        return os.path.basename(self.path)

    def relative_iterdir(self, relpath=''):
        seen = set()
        for name in os.listdir(str(self.path)):
            if name.startswith(relpath) and name not in seen:
                seen.add(name)
                yield name

    def open(self, relpath):
        return open(os.path.join(self.path, relpath))

    def mount(self, path):
        root = path
        return ArchiveRecord(root, root / self.VERSION_FILE,
                             self.uuid, self.version, self.framework_version)


class ArchiveCheck(_Archive):
    """Used by the Jupyter handlers"""

    # TODO: make this part of the archiver API at some point
    def open(self, relpath):
        abspath = os.path.join(str(self.path), relpath)
        return open(abspath, 'r')

    def relative_iterdir(self, relpath='.'):
        for p in pathlib.Path(self.path).iterdir():
            yield str(p.relative_to(self.path))

    def _get_uuid(self):
        return os.path.basename(self.path)


class Archiver:
    CURRENT_FORMAT_VERSION = '5'
    _FORMAT_REGISTRY = {
        # NOTE: add more archive formats as things change
        '0': 'qiime2.core.archive.format.v0:ArchiveFormat',
        '1': 'qiime2.core.archive.format.v1:ArchiveFormat',
        '2': 'qiime2.core.archive.format.v2:ArchiveFormat',
        '3': 'qiime2.core.archive.format.v3:ArchiveFormat',
        '4': 'qiime2.core.archive.format.v4:ArchiveFormat',
        '5': 'qiime2.core.archive.format.v5:ArchiveFormat'
    }

    @classmethod
    def _make_temp_path(cls, uuid):
        """Allocates a place in the cache for the file to be temporarily
        written. Returns this location and the cache in use.
        """
        from qiime2.core.cache import get_cache

        cache = get_cache()
        path = cache.process_pool._allocate(uuid)
        return path, cache

    @classmethod
    def _destroy_temp_path(cls, process_alias):
        from qiime2.core.cache import get_cache

        cache = get_cache()
        cache.process_pool.remove(str(process_alias))

    @classmethod
    def get_format_class(cls, version):
        try:
            imp, fmt_cls = cls._FORMAT_REGISTRY[version].split(':')
        except KeyError:
            return None
        return getattr(importlib.import_module(imp), fmt_cls)

    @classmethod
    def get_archive(cls, filepath):
        filepath = pathlib.Path(filepath)
        if not filepath.exists():
            raise ValueError("%s does not exist." % filepath)

        if _ZipArchive.is_archive_type(filepath):
            archive = _ZipArchive(filepath)
        elif _NoOpArchive.is_archive_type(filepath):
            archive = _NoOpArchive(filepath)
        else:
            raise ValueError("%s is not a QIIME archive." % filepath)

        return archive

    @classmethod
    def _futuristic_archive_error(cls, filepath, archive):
        raise ValueError("%s was created by 'QIIME %s'. The currently"
                         " installed framework cannot interpret archive"
                         " version %r."
                         % (filepath, archive.framework_version,
                            archive.version))

    @classmethod
    def peek(cls, filepath):
        archive = cls.get_archive(filepath)
        Format = cls.get_format_class(archive.version)
        if Format is None:
            cls._futuristic_archive_error(filepath, archive)
        # NOTE: in the future, we may want to manipulate the results so that
        # older formats provide the "new" API even if they don't support it.
        # e.g. a new format has a new property that peek should describe. We
        # add some compatability code here to return a default for that
        # property on older formats.
        return Format.load_metadata(archive)

    @classmethod
    def extract(cls, filepath, dest):
        archive = cls.get_archive(filepath)
        dest = os.path.join(dest, str(archive.uuid))
        os.makedirs(dest)
        # Format really doesn't matter, the archive knows how to extract so
        # that is sufficient, furthermore it would suck if something was wrong
        # with an archive's format and extract failed to actually extract.
        return str(archive.extract(dest))

    @classmethod
    def load(cls, filepath):
        archive = cls.get_archive(filepath)
        path, cache = cls._make_temp_path(archive.uuid)

        try:
            Format = cls.get_format_class(archive.version)
            if Format is None:
                cls._futuristic_archive_error(filepath, archive)

            archive.mount(path)
            process_alias, data_path = \
                cache._rename_to_data(archive.uuid, path)
            rec = ArchiveRecord(
                data_path, data_path / archive.VERSION_FILE, archive.uuid,
                archive.version, archive.framework_version)
            ref = cls(data_path, process_alias, Format(rec), cache)
            return ref
        # We really just want to kill these paths if anything at all goes wrong
        # Exceptions including keyboard interrupts are re-raised
        except:  # noqa: E722
            cls._destroy_temp_path(archive.uuid)
            if 'process_alias' in vars():
                cls._destroy_temp_path(process_alias)
            raise

    @classmethod
    def load_raw(cls, filepath, cache):
        archive = cls.get_archive(filepath)
        process_alias = cache._alias(str(archive.uuid))

        Format = cls.get_format_class(archive.version)
        if Format is None:
            cls._futuristic_archive_error(filepath, archive)

        path = pathlib.Path(filepath)

        rec = archive.mount(path)
        ref = cls(path, process_alias, Format(rec), cache)

        return ref

    @classmethod
    def from_data(cls, type, format, data_initializer, provenance_capture):
        uuid = _uuid.uuid4()
        path, cache = cls._make_temp_path(uuid)

        try:
            rec = _Archive.setup(uuid, path, cls.CURRENT_FORMAT_VERSION,
                                 qiime2.__version__)

            Format = cls.get_format_class(cls.CURRENT_FORMAT_VERSION)
            Format.write(rec, type, format, data_initializer,
                         provenance_capture)

            process_alias, data_path = cache._rename_to_data(uuid, path)
            rec = ArchiveRecord(data_path, data_path / _Archive.VERSION_FILE,
                                uuid, cls.CURRENT_FORMAT_VERSION,
                                qiime2.__version__)
            ref = cls(data_path, process_alias, Format(rec), cache)
            return ref
        # We really just want to kill these paths if anything at all goes wrong
        # Exceptions including keyboard interrupts are re-raised
        except:  # noqa: E722
            cls._destroy_temp_path(uuid)
            if 'process_alias' in vars():
                cls._destroy_temp_path(process_alias)
            raise

    def __init__(self, path, process_alias, fmt, cache):
        self.path = path
        self.process_alias = process_alias
        self._fmt = fmt
        self._destructor = weakref.finalize(self, cache._deallocate,
                                            str(self.process_alias))

    @property
    def uuid(self):
        return self._fmt.uuid

    @property
    def type(self):
        return self._fmt.type

    @property
    def format(self):
        return self._fmt.format

    @property
    def data_dir(self):
        return self._fmt.data_dir

    @property
    def root_dir(self):
        return self._fmt.path

    @property
    def provenance_dir(self):
        return getattr(self._fmt, 'provenance_dir', None)

    @property
    def citations(self):
        return getattr(self._fmt, 'citations', cite.Citations())

    def save(self, filepath):
        _ZipArchive.save(self.path, filepath)

    def validate_checksums(self):
        if not isinstance(self._fmt, self.get_format_class('5')):
            return ChecksumDiff({}, {}, {})

        obs = dict(x for x in md5sum_directory(str(self.root_dir)).items()
                   if x[0] != self._fmt.CHECKSUM_FILE)
        with open(self.root_dir / self._fmt.CHECKSUM_FILE) as fh:
            exp = dict(from_checksum_format(line) for line in
                       fh.readlines())
        obs_keys = set(obs)
        exp_keys = set(exp)

        added = {x: obs[x] for x in obs_keys - exp_keys}
        removed = {x: exp[x] for x in exp_keys - obs_keys}
        changed = {x: (exp[x], obs[x]) for x in exp_keys & obs_keys
                   if exp[x] != obs[x]}

        return ChecksumDiff(added=added, removed=removed, changed=changed)
