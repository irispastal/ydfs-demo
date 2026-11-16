"""Select the Linux libfuse 3 binding or the Windows WinFsp FUSE 3 binding."""
import errno
import os
import sys

if sys.platform == "linux":
    # mfusepy otherwise prefers libfuse 2 when both versions are installed.
    os.environ.setdefault("FUSE_LIBRARY_NAME", "fuse3")
    import mfusepy

    if mfusepy.fuse_version_major != 3:
        raise RuntimeError("YDFS requires libfuse 3 on Linux; check FUSE_LIBRARY_PATH/FUSE_LIBRARY_NAME")

    FuseOSError = mfusepy.FuseOSError
    Operations = mfusepy.Operations
    BACKEND = f"mfusepy / libfuse {mfusepy.fuse_version_major}.{mfusepy.fuse_version_minor}"

    class FUSE(mfusepy.FUSE):
        def rename_fuse_3(self, old, new, flags):
            # mfusepy 3.1.1 drops flags, which could overwrite a destination
            # despite RENAME_NOREPLACE. Do not claim unsupported atomic modes.
            if flags:
                raise FuseOSError(errno.EOPNOTSUPP)
            return super().rename_fuse_3(old, new, flags)

elif os.name == "nt":
    from adapter.winfsp3 import BACKEND, FUSE, FuseOSError, Operations
else:
    raise RuntimeError("YDFS supports Linux (libfuse 3) and Windows (WinFsp)")
