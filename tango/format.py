import bz2
import dataclasses
import gzip
import importlib
import json
import logging
import lzma
import mmap
from abc import abstractmethod
from os import PathLike
from pathlib import Path
from typing import (
    IO,
    Any,
    Callable,
    Dict,
    Generic,
    Iterable,
    Iterator,
    List,
    Optional,
    TypeVar,
    Union,
    cast,
)

import dill
import xxhash

from tango.common.aliases import PathOrStr
from tango.common.exceptions import ConfigurationError
from tango.common.logging import TangoLogger
from tango.common.registrable import Registrable

T = TypeVar("T")


class Format(Registrable, Generic[T]):
    """
    Formats write objects to directories and read them back out.

    In the context of Tango, the objects that are written by formats are usually
    the result of a :class:`~tango.step.Step`.
    """

    VERSION: int = NotImplemented
    """
    Formats can have versions. Versions are part of a step's unique signature, part of
    :attr:`~tango.step.Step.unique_id`, so when a step's format changes,
    that will cause the step to be recomputed.
    """

    default_implementation = "dill"

    @abstractmethod
    def write(self, artifact: T, dir: PathOrStr):
        """Writes the ``artifact`` to the directory at ``dir``."""
        raise NotImplementedError()

    @abstractmethod
    def read(self, dir: PathOrStr) -> T:
        """Reads an artifact from the directory at ``dir`` and returns it."""
        raise NotImplementedError()

    @abstractmethod
    def checksum(self, dir: PathOrStr) -> str:
        """
        Produces a checksum of the serialized artifact.

        Should raise :class:`FileNotFoundError` if the artifact can't be found.
        """
        raise NotImplementedError()

    @staticmethod
    def _checksum_artifact(path: PathOrStr) -> str:
        """
        A helper method that can be used to compute the checksum of an artifact.

        This can make implementing :meth:`checksum()` easier.
        """
        filepath = Path(path)
        if not filepath.is_file():
            raise FileNotFoundError(str(filepath))

        h = xxhash.xxh128()
        with filepath.open("rb") as f:
            with mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ) as m:
                h.update(m)
        return h.hexdigest()


_OPEN_FUNCTIONS: Dict[Optional[str], Callable[[PathLike, str], IO]] = {
    None: open,
    "None": open,
    "none": open,
    "null": open,
    "gz": gzip.open,  # type: ignore
    "gzip": gzip.open,  # type: ignore
    "bz": bz2.open,
    "bz2": bz2.open,
    "bzip": bz2.open,
    "bzip2": bz2.open,
    "lzma": lzma.open,
}

_SUFFIXES: Dict[Callable, str] = {
    open: "",
    gzip.open: ".gz",
    bz2.open: ".bz2",
    lzma.open: ".xz",
}


def _open_compressed(filename: PathOrStr, mode: str) -> IO:
    open_fn: Callable
    filename = str(filename)
    for open_fn, suffix in _SUFFIXES.items():
        if len(suffix) > 0 and filename.endswith(suffix):
            break
    else:
        open_fn = open
    return open_fn(filename, mode)


@Format.register("dill")
class DillFormat(Format[T], Generic[T]):
    """
    This format writes the artifact as a single file called "data.dill" using dill
    (a drop-in replacement for pickle). Optionally, it can compress the data.

    This is very flexible, but not always the fastest.

    .. tip::
        This format has special support for iterables. If you write an iterator, it will consume the
        iterator. If you read an iterator, it will read the iterator lazily.

    """

    VERSION = 1

    def __init__(self, compress: Optional[str] = None):
        try:
            self.open = _OPEN_FUNCTIONS[compress]
        except KeyError:
            raise ConfigurationError(f"The {compress} compression format does not exist.")

    def write(self, artifact: T, dir: PathOrStr):
        filename = self._get_artifact_path(dir)
        with self.open(filename, "wb") as f:
            pickler = dill.Pickler(file=f)
            pickler.dump(self.VERSION)
            if hasattr(artifact, "__next__"):
                pickler.dump(True)
                for item in cast(Iterable, artifact):
                    pickler.dump(item)
            else:
                pickler.dump(False)
                pickler.dump(artifact)

    def read(self, dir: PathOrStr) -> T:
        filename = self._get_artifact_path(dir)
        with self.open(filename, "rb") as f:
            unpickler = dill.Unpickler(file=f)
            version = unpickler.load()
            if version > self.VERSION:
                raise ValueError(
                    f"File {filename} is too recent for this version of {self.__class__}."
                )
            iterator = unpickler.load()
            if iterator:
                return DillFormatIterator(filename)  # type: ignore
            else:
                return unpickler.load()

    def checksum(self, dir: PathOrStr) -> str:
        path = self._get_artifact_path(dir)
        return self._checksum_artifact(path)

    def _get_artifact_path(self, dir: PathOrStr) -> Path:
        return Path(dir) / ("data.dill" + _SUFFIXES[self.open])


class DillFormatIterator(Iterator[T], Generic[T]):
    """
    An ``Iterator`` class that is used so we can return an iterator from ``DillFormat.read()``.
    """

    def __init__(self, filename: PathOrStr):
        self.f: Optional[IO[Any]] = _open_compressed(filename, "rb")
        self.unpickler = dill.Unpickler(self.f)
        version = self.unpickler.load()
        if version > DillFormat.VERSION:
            raise ValueError(f"File {filename} is too recent for this version of {self.__class__}.")
        iterator = self.unpickler.load()
        if not iterator:
            raise ValueError(
                f"Tried to open {filename} as an iterator, but it does not store an iterator."
            )

    def __iter__(self) -> Iterator[T]:
        return self

    def __next__(self) -> T:
        if self.f is None:
            raise StopIteration()
        try:
            return self.unpickler.load()
        except EOFError:
            self.f.close()
            self.f = None
            raise StopIteration()


@Format.register("json")
class JsonFormat(Format[T], Generic[T]):
    """This format writes the artifact as a single file in json format.
    Optionally, it can compress the data. This is very flexible, but not always the fastest.

    .. tip::
        This format has special support for iterables. If you write an iterator, it will consume the
        iterator. If you read an iterator, it will read the iterator lazily.

    """

    VERSION = 2

    def __init__(self, compress: Optional[str] = None):
        self.logger = cast(TangoLogger, logging.getLogger(self.__class__.__name__))
        try:
            self.open = _OPEN_FUNCTIONS[compress]
        except KeyError:
            raise ConfigurationError(f"The {compress} compression format does not exist.")

    @staticmethod
    def _encoding_fallback(unencodable: Any):
        try:
            import torch

            if isinstance(unencodable, torch.Tensor):
                if len(unencodable.shape) == 0:
                    return unencodable.item()
                else:
                    raise TypeError(
                        "Tensors must have 1 element and no dimensions to be JSON serializable."
                    )
        except ImportError:
            pass

        if dataclasses.is_dataclass(unencodable):
            result = dataclasses.asdict(unencodable)
            module = type(unencodable).__module__
            qualname = type(unencodable).__qualname__
            if module == "builtins":
                result["_dataclass"] = qualname
            else:
                result["_dataclass"] = [module, qualname]
            return result

        raise TypeError(f"Object of type {type(unencodable)} is not JSON serializable")

    @staticmethod
    def _decoding_fallback(o: Dict) -> Any:
        if "_dataclass" in o:
            classname: Union[str, List[str]] = o.pop("_dataclass")
            if isinstance(classname, list) and len(classname) == 2:
                module, classname = classname
                constructor: Callable = importlib.import_module(module)  # type: ignore
                for item in classname.split("."):
                    constructor = getattr(constructor, item)
            elif isinstance(classname, str):
                constructor = globals()[classname]
            else:
                raise RuntimeError(f"Could not parse {classname} as the name of a dataclass.")
            return constructor(**o)
        return o

    def write(self, artifact: T, dir: PathOrStr):
        if hasattr(artifact, "__next__"):
            filename = self._get_artifact_path(dir, iterator=True)
            with self.open(filename, "wt") as f:
                for item in cast(Iterable, artifact):
                    json.dump(item, f, default=self._encoding_fallback)
                    f.write("\n")
        else:
            filename = self._get_artifact_path(dir, iterator=False)
            with self.open(filename, "wt") as f:
                json.dump(artifact, f, default=self._encoding_fallback)

    def read(self, dir: PathOrStr) -> T:
        iterator_filename = self._get_artifact_path(dir, iterator=True)
        iterator_exists = iterator_filename.exists()
        non_iterator_filename = self._get_artifact_path(dir, iterator=False)
        non_iterator_exists = non_iterator_filename.exists()

        if iterator_exists and non_iterator_exists:
            self.logger.warning(
                "Both %s and %s exist. Ignoring %s.",
                iterator_filename,
                non_iterator_filename,
                iterator_filename,
            )
            iterator_exists = False

        if not iterator_exists and not non_iterator_exists:
            raise IOError("Attempting to read non-existing data from %s", dir)
        if iterator_exists and not non_iterator_exists:
            return JsonFormatIterator(iterator_filename)  # type: ignore
        elif not iterator_exists and non_iterator_exists:
            with self.open(non_iterator_filename, "rt") as f:
                return json.load(f, object_hook=self._decoding_fallback)
        else:
            raise RuntimeError("This should be impossible.")

    def checksum(self, dir: PathOrStr) -> str:
        iterator_filename = self._get_artifact_path(dir, iterator=True)
        non_iterator_filename = self._get_artifact_path(dir, iterator=False)
        if iterator_filename.exists():
            return self._checksum_artifact(iterator_filename)
        else:
            return self._checksum_artifact(non_iterator_filename)

    def _get_artifact_path(self, dir: PathOrStr, iterator: bool = False) -> Path:
        return Path(dir) / (("data.jsonl" if iterator else "data.json") + _SUFFIXES[self.open])


class JsonFormatIterator(Iterator[T], Generic[T]):
    """
    An ``Iterator`` class that is used so we can return an iterator from ``JsonFormat.read()``.
    """

    def __init__(self, filename: PathOrStr):
        self.f: Optional[IO[Any]] = _open_compressed(filename, "rt")

    def __iter__(self) -> Iterator[T]:
        return self

    def __next__(self) -> T:
        if self.f is None:
            raise StopIteration()
        try:
            line = self.f.readline()
            if len(line) <= 0:
                raise EOFError()
            return json.loads(line, object_hook=JsonFormat._decoding_fallback)
        except EOFError:
            self.f.close()
            self.f = None
            raise StopIteration()
