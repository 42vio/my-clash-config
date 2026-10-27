"""Lightweight shared operation errors and process locking."""

import fcntl
import os
import stat
from pathlib import Path

class ServiceError(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__(code)

class _OperationLock:
    def __init__(self, path): self.path, self.descriptor = Path(path), None
    def __enter__(self):
        root = self.path.parent
        if not root.is_absolute() or any(part in {".", ".."} for part in root.parts): raise ServiceError("operation_lock_invalid")
        opened=[]; parent=None
        try:
            parent=os.open(root.anchor,os.O_RDONLY|os.O_DIRECTORY)
            for part in root.parts[1:]:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0),dir_fd=parent)
                opened.append(parent); parent=child
                details=os.fstat(parent)
                if not stat.S_ISDIR(details.st_mode): raise OSError
            details=os.fstat(parent)
            if stat.S_IMODE(details.st_mode)!=0o700 or details.st_uid not in {0,os.geteuid()}: raise OSError
            flags=os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_CLOEXEC",0)
            self.descriptor=os.open("operation.lock",flags,0o600,dir_fd=parent); os.fchmod(self.descriptor,0o600); details=os.fstat(self.descriptor)
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode)!=0o600 or details.st_nlink!=1 or details.st_uid not in {0,os.geteuid()}: raise OSError
        except OSError:
            if isinstance(self.descriptor,int):
                try:os.close(self.descriptor)
                except OSError:pass
            self.descriptor=None
            raise ServiceError("operation_lock_invalid") from None
        finally:
            for descriptor in opened:
                try:os.close(descriptor)
                except OSError:pass
            if parent is not None:
                try:os.close(parent)
                except OSError:pass
        try: fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try: os.close(self.descriptor)
            except OSError: pass
            finally: self.descriptor = None
            raise ServiceError("operation_busy") from None
        return self
    def __exit__(self, exc_type, *_):
        if self.descriptor is None:return
        error=False
        try: fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        except OSError:error=True
        finally:
            try:os.close(self.descriptor)
            except OSError:error=True
            self.descriptor=None
        if error and exc_type is None: raise ServiceError("operation_lock_invalid")
