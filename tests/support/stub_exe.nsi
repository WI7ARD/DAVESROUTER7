; Stand-in for the bundled executables in installer tests (built with makensis, so no
; C compiler is needed). Appends its command line to a log and exits immediately.
Unicode true
Target amd64-unicode
SilentInstall silent
RequestExecutionLevel user
OutFile "${OUTFILE}"
Section
  FileOpen $0 "$LOCALAPPDATA\pcbrouter-stub-calls.log" a
  FileSeek $0 0 END
  FileWrite $0 "$CMDLINE$\r$\n"
  FileClose $0
SectionEnd
