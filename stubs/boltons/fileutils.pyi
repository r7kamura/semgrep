from typing import Optional, Type

class FilePerms:
    def __init__(self, user: str = ..., group: str = ..., other: str = ...) -> None: ...
    def __int__(self) -> int: ...
    def __repr__(self) -> str: ...

    class _FilePermProperty:
        def __get__(
            self, fp_obj: FilePerms, type_: Optional[Type[FilePerms]] = ...
        ) -> str: ...
        def __set__(self, fp_obj: FilePerms, value: str) -> None: ...
        def _update_integer(self, fp_obj: FilePerms, value: str) -> None: ...
