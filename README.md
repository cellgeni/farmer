# farmer

Slack bot that tells you the status of your LSF jobs

## Usage

Farmer currently has two functions: listing pending/running jobs, and notifying you when your jobs finish.

To see your pending/running jobs, send Farmer a message saying "jobs". (You can also see jobs for other users, e.g. by saying "jobs for cellgeni-su".)

To be notified when jobs finish, you can use the included post-exec script:

```console
$ bsub ... -Ep /software/cellgen/cellgeni/etc/notify-slack.sh
```

or as a bsub comment:

```
#BSUB -Ep /software/cellgen/cellgeni/etc/notify-slack.sh
```

If you submit an array job with this post-exec script, you'll only be notified once every job in the array has finished.

## Preemptively Answered Questions

- **I don't use bsub, I use Nextflow** – Nextflow has built-in support for sending notifications when jobs finish. For example: `nextflow run -N you@email.example`

- **I don't use bsub, I use wr** – when adding a command to wr, use a command line like: `wr add --on_exit '[{"run": "/software/cellgen/cellgeni/etc/notify-slack.sh"}, {"cleanup": true}]'`

  If you want to be notified only when a _group_ of wr jobs has completed, add a job to send the notification:

  ```console
  $ echo /software/cellgen/cellgeni/etc/notify-slack.sh | wr add --deps notify
  ```
  
  Then add your jobs to the `notify` dependency group, using a command like `wr add --dep_grps notify`.

  (Note that you only need to run `wr add --deps notify` once – it will automatically run again if you add more commands with `--dep_grps notify` in the future.)

- **I run jobs using a service account/I need Slack notifications to go to someone else** – by default, Farmer tries to find a Slack account belonging to the job's owner, but you can override this heuristic in two ways.

  One option is to set the environment variable `FARMER_SLACK_USER` to your username: for example, put `export FARMER_SLACK_USER=zz0` into your service account's `~/.bashrc`.
  This might be useful if you run many jobs using this service account.

  Alternatively, you can pass a username straight to the post-exec script, by adding quotes (this overrides the environment variable, if set):

  ```console
  $ bsub ... -Ep "/software/cellgen/cellgeni/etc/notify-slack.sh zz0"
  ```

## Development

See [HACKING.md](HACKING.md).
