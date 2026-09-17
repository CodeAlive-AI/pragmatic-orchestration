# Terraform: private EC2 agent host (Linux)

This module creates one Ubuntu EC2 worker with:

- no security-group ingress;
- no public IP by default;
- SSH key installed for SSH-over-SSM;
- `AmazonSSMManagedInstanceCore` instance role;
- encrypted gp3 root disk;
- Node.js 22 plus caller-supplied bootstrap commands (agent CLIs, toolchains);
- user lingering enabled for persistent user services.

It does not create a VPC, subnet, NAT gateway, VPC endpoints, DNS, or a
repository checkout. The selected subnet must provide the outbound
connectivity your workload needs. For a fully private subnet, combine Systems
Manager endpoints with NAT/proxy egress for model APIs, Git/package hosts,
DNS, and time.

## Apply

```bash
ssh-keygen -t ed25519 -f ~/.ssh/agent-linux_ed25519

cat >terraform.tfvars <<EOF2
vpc_id         = "vpc-..."
subnet_id      = "subnet-..."
ssh_public_key = "$(cat ~/.ssh/agent-linux_ed25519.pub)"
bootstrap_commands = [
  "npm install -g <agent-cli-package>",
]
EOF2

terraform init
terraform plan
terraform apply
terraform output -raw ssh_config
```

Add the printed `ssh_config` block to `~/.ssh/config`, then authenticate the
agent CLIs over `ssh -t` (`porch-remote.md`).
