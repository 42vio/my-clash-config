# Private Data Handling

Copy the committed example files from `config/` into `private/config/` before creating live runtime settings.

Directories under `private/` use mode `0700`, and files under `private/` use mode `0600`.

Current release pointers use relative symlinks so switches stay atomic within the private root.

Reference configurations under `private/reference-configs/` are permanent records.

Neither Git nor backups should move private runtime data outside administrator-controlled encrypted storage.
