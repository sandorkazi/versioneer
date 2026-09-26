# bash completion for versioneer / vers (full flags/subcommands).
# Enable: source installer/completions/versioneer.bash
_versioneer_complete() {
  local cur prev words cword
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  words=("${COMP_WORDS[@]}")

  local top="init config target status diff log commit push pull deploy service bootstrap watch manifest doctor uninstall"
  local config_sub="create list show remove"
  local target_sub="add list remove set"
  local service_sub="install enable disable check run"

  # third word on: versioneer <cmd> <sub> ...
  if [[ ${#words[@]} -ge 3 ]]; then
    case "${words[1]}" in
      config)
        if [[ ${#words[@]} -eq 3 ]]; then
          COMPREPLY=($(compgen -W "$config_sub" -- "$cur"))
          return 0
        fi
        case "${words[2]}" in
          create) COMPREPLY=($(compgen -W "--name --path --upstream --auto-commit --auto-push --check-interval --notify --no-notify -h --help" -- "$cur")); return 0 ;;
        esac
        ;;
      target)
        if [[ ${#words[@]} -eq 3 ]]; then
          COMPREPLY=($(compgen -W "$target_sub" -- "$cur"))
          return 0
        fi
        case "${words[2]}" in
          add) COMPREPLY=($(compgen -W "--root --kind --flex --interest --glob --auto-add-glob --ignore --symlink --machines --retention --retention-count --retention-age --template --no-template --on-deploy --deploy-path --check-interval --encrypt --no-encrypt --auto-commit --no-auto-commit --force -h --help" -- "$cur")); return 0 ;;
          set) COMPREPLY=($(compgen -W "--auto-commit --no-auto-commit --retention --retention-count --retention-age --clear-retention -h --help" -- "$cur")); return 0 ;;
        esac
        ;;
      service)
        if [[ ${#words[@]} -eq 3 ]]; then
          COMPREPLY=($(compgen -W "$service_sub" -- "$cur"))
          return 0
        fi
        case "${words[2]}" in
          install) COMPREPLY=($(compgen -W "--enable --no-enable -h --help" -- "$cur")); return 0 ;;
          check) COMPREPLY=($(compgen -W "--all --host -h --help" -- "$cur")); return 0 ;;
          run) COMPREPLY=($(compgen -W "--once --host -h --help" -- "$cur")); return 0 ;;
        esac
        ;;
      deploy) COMPREPLY=($(compgen -W "--dry-run --plan-out --plan --to --yes --no-interaction --force-host --host --prune --apply -h --help" -- "$cur")); return 0 ;;
      bootstrap) COMPREPLY=($(compgen -W "--all --to --host --dry-run --yes -h --help" -- "$cur")); return 0 ;;
      watch) COMPREPLY=($(compgen -W "--auto-add --root --ignore --glob --timeout -h --help" -- "$cur")); return 0 ;;
      manifest) COMPREPLY=($(compgen -W "--packages --wine --systemd --env -h --help" -- "$cur")); return 0 ;;
      doctor) COMPREPLY=($(compgen -W "--secrets -h --help" -- "$cur")); return 0 ;;
      commit) COMPREPLY=($(compgen -W "-m --message --all --prune-retention -h --help" -- "$cur")); return 0 ;;
      status|log) COMPREPLY=($(compgen -W "--host -n --number -h --help" -- "$cur")); return 0 ;;
      uninstall) COMPREPLY=($(compgen -W "--purge-stores --yes --venv -h --help" -- "$cur")); return 0 ;;
    esac
  fi

  # global flags + top-level commands
  if [[ "$cur" == -* ]]; then
    COMPREPLY=($(compgen -W "-C --config --version -h --help" -- "$cur"))
  else
    COMPREPLY=($(compgen -W "$top" -- "$cur"))
  fi
}
complete -F _versioneer_complete versioneer vers
