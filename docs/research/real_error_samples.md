# Real error samples (research, 2026-10-08)

Copied from fetched public pages by the research subagent; each sample lists its source. Not yet added to `error_catalog.json`. Unverified beyond the source page. Some pasted output contains `<redacted>` fields from the original reporter.

## Terraform

Undeclared variable (https://github.com/hashicorp/terraform/issues/28764):
```
Error: Value for undeclared variable

A variable named "some_var" was assigned on the command line, but the root
module does not declare a variable of that name. To use this value, add a
"variable" block to the configuration.
```

Inconsistent dependency lock file, no version selected (https://github.com/hashicorp/terraform-provider-hashicups/issues/132; local-dev case, not CI):
```
│ Error: Inconsistent dependency lock file
│ The following dependency selections recorded in the lock file are inconsistent with the current configuration:
│   - provider hashicorp.com/edu/hashicups: required by this configuration but no version is selected
│ To make the initial dependency selections that will initialize the dependency lock file, run:
│   terraform init
```

Inconsistent dependency lock file, saved plan (https://github.com/gruntwork-io/terragrunt/issues/2646):
```
│ Error: Inconsistent dependency lock file
│ The given plan file was created with a different set of external dependency
│ selections than the current configuration. A saved plan can be applied only
│ to the same configuration it was created from.
│ Create a new plan from the updated configuration.
```

Locked provider mismatch (https://github.com/hashicorp/terraform/issues/38496):
```
Error: Failed to query available provider packages
Could not retrieve the list of available versions for provider example.com/example/example: locked provider
example.com/example/example 4.52.1-example does not match configured version constraint ; must use terraform init -upgrade to allow
selection of new versions
```

## GitHub Actions

Unresolvable action (https://github.internet2.edu/Ioannis/registry/actions/runs/5891, GHES):
```
Unable to resolve action `shivammathur/setup-php@v2`, repository not found on this server. If you want to use this action from GitHub.com, see the following documentation: https://docs.github.com/en/enterprise/admin/github-actions/managing-access-to-actions-from-githubcom
```

Environment protection (https://github.com/orgs/community/discussions/111160):
```
Deployment Protection Rule Comment: Required reviewers protection rule deleted
Annotation Failure Reason: The deployment was rejected or didn't satisfy other protection rules.
```

No verbatim body found for reusable-workflow not found (discussion 151541 returned a stub; title only: "Failed to fetch workflow: workflow was not found.").

## Kubernetes

OOMKilled (https://github.com/Alluxio/alluxio/issues/13976):
```
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Tue, 24 Aug 2021 09:56:35 -0700
      Finished:     Tue, 24 Aug 2021 11:40:27 -0700
    Restart Count:  3
    Limits:
      cpu:     4
      memory:  120G
```

ImagePullBackOff (https://github.com/kubernetes/kubernetes/issues/83622):
```
Normal   Pulling  23m (x4 over 25m)   kubelet  Pulling image "some-nonexistent-image"
Warning  Failed   23m (x4 over 25m)   <redacted>  Failed to pull image "some-nonexistent-image": rpc error: code = Unknown desc = Error response from daemon: pull access denied for some-nonexistent-image, repository does not exist or may require 'docker login'
Warning  Failed   23m (x4 over 25m)   kubelet  Error: ErrImagePull
Normal   BackOff  10m (x67 over 25m)  kubelet  Back-off pulling image "some-nonexistent-image"
Warning  Failed   2s (x112 over 25m)  kubelet  Error: ImagePullBackOff
```

FailedScheduling (https://github.com/pires/kubernetes-elasticsearch-cluster/issues/178):
```
Warning  FailedScheduling  <invalid> (x13 over 2m)  default-scheduler  0/5 nodes are available: 5 Insufficient cpu.
```

Evicted (https://community.veeam.com/veeam-kasten-kubernetes-data-protection-support-92/the-node-was-low-on-resource-ephemeral-storage-4465):
```
Warning  FailedScheduling  5m29s  default-scheduler  0/4 nodes are available: 1 node(s) had untolerated taint {node-role.kubernetes.io/master: }, 3 node(s) had untolerated taint {node.kubernetes.io/disk-pressure: }. preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling.
Warning  Evicted              20s    kubelet  The node was low on resource: ephemeral-storage.
Warning  ExceededGracePeriod  10s    kubelet  Container runtime did not kill the pod within specified grace period.
```

## npm

ECONNRESET (https://github.com/npm/cli/issues/8015):
```
npm ERR! code ECONNRESET
npm ERR! errno ECONNRESET
npm ERR! network Invalid response body while trying to fetch https://registry.npmjs.org/@types%2fnode: aborted
npm ERR! network This is a problem related to network connectivity.
npm ERR! network In most cases you are behind a proxy or have bad network settings.
```

ETIMEDOUT (https://github.com/npm/cli/issues/7076):
```
npm ERR! code ETIMEDOUT
npm ERR! syscall connect
npm ERR! errno ETIMEDOUT
npm ERR! network request to https://registry.npmjs.org/yargs-parser/-/yargs-parser-20.2.9.tgz failed, reason: connect ETIMEDOUT 104.16.24.34:443
```

## pip

(https://github.com/pypa/pip/issues/10137, GitHub Actions; version list elided by the reporter/agent):
```
$ pip install 'black>=21'
ERROR: Could not find a version that satisfies the requirement black>=21 (from versions: 18.3a0, ... 21.5b2, 21.6b0)
ERROR: No matching distribution found for black>=21
```

(https://github.com/tensorflow/tensorflow/issues/83097):
```
ERROR: No matching distribution found for tensorflow==2.1.0
ERROR: Could not find a version that satisfies the requirement tensorflow (from versions: none)
```

## Docker Hub rate limit

(https://github.com/docker/setup-buildx-action/issues/395):
```
Error: ERROR: Error response from daemon: toomanyrequests: You have reached your pull rate limit. You may increase the limit by authenticating and upgrading: https://www.docker.com/increase-rate-limit
```

## AWS

Throttling (https://github.com/aws/aws-cli/issues/4635 and /2948):
```
An error occurred (ThrottlingException) when calling the DescribeLogStreams operation (reached max retries: 4): Rate exceeded
An error occurred (Throttling) when calling the DescribeStacks operation (reached max retries: 4): Rate exceeded
```

ExpiredToken (https://github.com/aws/aws-cli/issues/7870):
```
An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: The security token included in the request is expired
```

## DNS

(https://github.com/crowdin/github-action/issues/98):
```
fatal: unable to access 'https://github.com/companyname/project.git/': Could not resolve host: github.com
```
(https://github.com/community/community/discussions/31487):
```
ssh: Could not resolve hostname github.com: Temporary failure in name resolution
fatal: Could not read from remote repository.
```

## Gaps

- No verbatim "unable to find version" log text for a missing `v` in an action ref (only described in https://redirect.github.com/coverallsapp/github-action/pull/210).
- No CI-hosted Terraform lock-file sample; no official label for a timed-out environment approval.
- Not found: the flaky-test deterministic signature, which would resolve the invariant 3 label conflict.
