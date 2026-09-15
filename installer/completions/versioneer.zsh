#compdef versioneer
# zsh completion stub (Phase 0)
# Enable: fpath+=(installer/completions); compinit
_versioneer() {
  local -a cmds=(config target status diff log commit push pull deploy service bootstrap watch manifest doctor)
  _describe 'command' cmds
}
compdef _versioneer versioneer
