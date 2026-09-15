# bash completion stub (Phase 0) — full completions land with Phase 6.
# Enable: source installer/completions/versioneer.bash
_versioneer_complete() {
  local cmds="config target status diff log commit push pull deploy service bootstrap watch manifest doctor"
  COMPREPLY=($(compgen -W "$cmds" -- "${COMP_WORDS[COMP_CWORD]}"))
}
complete -F _versioneer_complete versioneer
