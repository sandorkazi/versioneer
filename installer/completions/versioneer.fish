# fish completion for versioneer / vers (full flags/subcommands).
# Enable: source installer/completions/versioneer.fish
for prog in versioneer vers
  for cmd in init config target status diff log commit push pull deploy service bootstrap watch manifest doctor uninstall
    complete -c $prog -f -n __fish_use_subcommand -a $cmd
  end
  # subcommands
  complete -c $prog -f -n '__fish_seen_subcommand_from config' -a "create list show remove"
  complete -c $prog -f -n '__fish_seen_subcommand_from target' -a "add list remove set"
  complete -c $prog -f -n '__fish_seen_subcommand_from service' -a "install enable disable check run"
  # global
  complete -c $prog -s C -l config -d 'Config name' -r
  # config create
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l name -r
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l path -r
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l upstream -r
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l auto-commit
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l auto-push
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l check-interval -r
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l notify
  complete -c $prog -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l no-notify
  # target add
  for flag in root kind flex interest glob auto-add-glob ignore symlink machines retention retention-count retention-age template no-template on-deploy deploy-path check-interval encrypt no-encrypt auto-commit no-auto-commit force remote-root remote-backend remote-retention
    complete -c $prog -f -n '__fish_seen_subcommand_from target; and __fish_seen_subcommand_from add' -l $flag
  end
  # target set (toggle autocommittability / retention on tracked files)
  for flag in auto-commit no-auto-commit retention retention-count retention-age clear-retention remote-root remote-retention clear-remote
    complete -c $prog -f -n '__fish_seen_subcommand_from target; and __fish_seen_subcommand_from set' -l $flag
  end
  # service
  complete -c $prog -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from install' -l enable
  complete -c $prog -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from install' -l no-enable
  complete -c $prog -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from check' -l all
  complete -c $prog -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from check' -l host -r
  complete -c $prog -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from run' -l once
  complete -c $prog -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from run' -l host -r
  # deploy
  for flag in dry-run plan-out plan to yes no-interaction force-host host prune apply
    complete -c $prog -f -n '__fish_seen_subcommand_from deploy' -l $flag
  end
  # bootstrap
  for flag in all to host dry-run yes
    complete -c $prog -f -n '__fish_seen_subcommand_from bootstrap' -l $flag
  end
  # watch
  for flag in auto-add root ignore glob timeout
    complete -c $prog -f -n '__fish_seen_subcommand_from watch' -l $flag
  end
  # manifest
  for flag in packages wine systemd env
    complete -c $prog -f -n '__fish_seen_subcommand_from manifest' -l $flag
  end
  # doctor / commit / status-log / uninstall
  complete -c $prog -f -n '__fish_seen_subcommand_from doctor' -l secrets
  complete -c $prog -f -n '__fish_seen_subcommand_from commit' -s m -l message -r
  complete -c $prog -f -n '__fish_seen_subcommand_from commit' -l all
  complete -c $prog -f -n '__fish_seen_subcommand_from commit' -l prune-retention
  complete -c $prog -f -n '__fish_seen_subcommand_from status' -l host -r
  complete -c $prog -f -n '__fish_seen_subcommand_from log' -l host -r
  complete -c $prog -f -n '__fish_seen_subcommand_from log' -s n -l number -r
  complete -c $prog -f -n '__fish_seen_subcommand_from uninstall' -l purge-stores
  complete -c $prog -f -n '__fish_seen_subcommand_from uninstall' -l yes
  complete -c $prog -f -n '__fish_seen_subcommand_from uninstall' -l venv -r
end
