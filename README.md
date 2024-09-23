# farmer

Slack bot that tells you the status of your LSF jobs

## Usage

To be notified when jobs finish, you can use the included post-exec script:

```console
$ bsub ... -Ep /path/to/farmer/src/post-exec.sh
```

## Preemptively Answered Questions

- **I don't use bsub, I use Nextflow** – Nextflow has built-in support for sending notifications when jobs finish. For example: `nextflow run -N you@email.example`

- **I don't use bsub, I use wr** – when adding a command to wr, use a command line like: `wr add --on_exit '[{"run": "/path/to/farmer/src/post-exec.sh"}, {"cleanup": true}]'`

  If you want to be notified only when a _group_ of wr jobs has completed, add a job to send the notification:

  ```console
  $ echo /path/to/farmer/src/post-exec.sh | wr add --deps notify
  ```
  
  Then add your jobs to the `notify` dependency group, using a command like `wr add --dep_grps notify`.
  
  Note that you only need to run the first command once – it will automatically notify you again if you add more commands with `--dep_grps notify` in the future.

## Development

See [HACKING.md](HACKING.md).
