#compdef versioneer
# zsh completion for versioneer (full flags/subcommands).
# Enable: fpath+=(installer/completions); compinit
_versioneer() {
  local -a cmds=(config target status diff log commit push pull deploy service bootstrap watch manifest doctor uninstall)
  local -a config_sub=(create list show remove)
  local -a target_sub=(add list remove)
  local -a service_sub=(install enable disable check run)
  _arguments -C \
    '-C[config name]:config:' '--config[config name]:config:' \
    '--version' '(-h --help)'{-h,--help}'[help]' \
    '1: :->cmd' '*:: :->args'
  case $state in
    cmd) _describe 'command' cmds ;;
    args)
      case ${words[2]} in
        config)
          _describe 'config subcommand' config_sub
          case ${words[3]} in
            create) _arguments '--name[name]:' '--path[path]:' '--upstream[url]:' '--auto-commit' '--auto-push' '--check-interval[interval]:' '--notify' '--no-notify' ;;
          esac ;;
        target)
          _describe 'target subcommand' target_sub
          case ${words[3]} in
            add) _arguments '--root[root]:' '--kind[kind]:(text binary dir auto)' '--flex[flex]:(fixed user flexi auto)' '--interest[interest]:(state diff)' '--glob[glob]:' '--auto-add-glob[glob]:' '--ignore[pattern]:' '--symlink[symlink]:(preserve follow)' '--machines[hosts]:' '--retention[count]:' '--retention-count[count]:' '--retention-age[age]:' '--template' '--no-template' '--on-deploy[cmd]:' '--deploy-path[path]:' '--check-interval[interval]:' '--encrypt' '--no-encrypt' '--force' ;;
          esac ;;
        service)
          _describe 'service subcommand' service_sub
          case ${words[3]} in
            install) _arguments '--enable' '--no-enable' ;;
            check) _arguments '--all' '--host[host]:' ;;
            run) _arguments '--once' '--host[host]:' ;;
          esac ;;
        deploy) _arguments '--dry-run' '--plan-out[file]:' '--plan[file]:' '--to[path]:' '--yes' '--no-interaction' '--force-host' '--host[host]:' '--prune' '--apply' ;;
        bootstrap) _arguments '--all' '--to[dir]:' '--host[host]:' '--dry-run' '--yes' ;;
        watch) _arguments '--auto-add' '--root[root]:' '--ignore[pattern]:' '--glob[glob]:' '--timeout[secs]:' ;;
        manifest) _arguments '--packages' '--wine' '--systemd' '--env' ;;
        doctor) _arguments '--secrets' ;;
        commit) _arguments '-m[message]:' '--message[message]:' '--all' '--prune-retention' ;;
        status) _arguments '--host[host]:' ;;
        log) _arguments '--host[host]:' '-n[number]:' '--number[number]:' ;;
        uninstall) _arguments '--purge-stores' '--yes' '--venv[dir]:' ;;
      esac ;;
  esac
}
compdef _versioneer versioneer
