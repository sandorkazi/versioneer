# fish completion stub (Phase 0)
# Enable: source installer/completions/versioneer.fish
for cmd in config target status diff log commit push pull deploy service bootstrap watch manifest doctor
  complete -c versioneer -f -n __fish_use_subcommand -a $cmd
end
