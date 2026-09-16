# fish completion for versioneer (full flags/subcommands).
# Enable: source installer/completions/versioneer.fish
for cmd in config target status diff log commit push pull deploy service bootstrap watch manifest doctor uninstall
  complete -c versioneer -f -n __fish_use_subcommand -a $cmd
end
# subcommands
complete -c versioneer -f -n '__fish_seen_subcommand_from config' -a "create list show remove"
complete -c versioneer -f -n '__fish_seen_subcommand_from target' -a "add list remove"
complete -c versioneer -f -n '__fish_seen_subcommand_from service' -a "install enable disable check run"
# global
complete -c versioneer -s C -l config -d 'Config name' -r
# config create
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l name -r
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l path -r
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l upstream -r
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l auto-commit
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l auto-push
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l check-interval -r
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l notify
complete -c versioneer -f -n '__fish_seen_subcommand_from config; and __fish_seen_subcommand_from create' -l no-notify
# target add
for flag in root kind flex interest glob auto-add-glob ignore symlink machines retention retention-count retention-age template no-template on-deploy deploy-path check-interval encrypt no-encrypt force
  complete -c versioneer -f -n '__fish_seen_subcommand_from target; and __fish_seen_subcommand_from add' -l $flag
end
# service
complete -c versioneer -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from install' -l enable
complete -c versioneer -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from install' -l no-enable
complete -c versioneer -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from check' -l all
complete -c versioneer -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from check' -l host -r
complete -c versioneer -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from run' -l once
complete -c versioneer -f -n '__fish_seen_subcommand_from service; and __fish_seen_subcommand_from run' -l host -r
# deploy
for flag in dry-run plan-out plan to yes no-interaction force-host host prune apply
  complete -c versioneer -f -n '__fish_seen_subcommand_from deploy' -l $flag
end
# bootstrap
for flag in all to host dry-run yes
  complete -c versioneer -f -n '__fish_seen_subcommand_from bootstrap' -l $flag
end
# watch
for flag in auto-add root ignore glob timeout
  complete -c versioneer -f -n '__fish_seen_subcommand_from watch' -l $flag
end
# manifest
for flag in packages wine systemd env
  complete -c versioneer -f -n '__fish_seen_subcommand_from manifest' -l $flag
end
# doctor / commit / status-log / uninstall
complete -c versioneer -f -n '__fish_seen_subcommand_from doctor' -l secrets
complete -c versioneer -f -n '__fish_seen_subcommand_from commit' -s m -l message -r
complete -c versioneer -f -n '__fish_seen_subcommand_from commit' -l all
complete -c versioneer -f -n '__fish_seen_subcommand_from commit' -l prune-retention
complete -c versioneer -f -n '__fish_seen_subcommand_from status' -l host -r
complete -c versioneer -f -n '__fish_seen_subcommand_from log' -l host -r
complete -c versioneer -f -n '__fish_seen_subcommand_from log' -s n -l number -r
complete -c versioneer -f -n '__fish_seen_subcommand_from uninstall' -l purge-stores
complete -c versioneer -f -n '__fish_seen_subcommand_from uninstall' -l yes
complete -c versioneer -f -n '__fish_seen_subcommand_from uninstall' -l venv -r
