output "instance_id" {
  value       = aws_instance.this.id
  description = "Use as HostName in the SSH-over-SSM alias."
}

output "private_ip" {
  value = aws_instance.this.private_ip
}

output "iam_role_name" {
  value = aws_iam_role.this.name
}

output "security_group_id" {
  value = aws_security_group.this.id
}

output "ssh_config" {
  value = <<-EOT
  Host agent-linux
    HostName ${aws_instance.this.id}
    User ubuntu
    IdentityFile ~/.ssh/agent-linux_ed25519
    IdentitiesOnly yes
    ProxyCommand sh -c "aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p' --region <AWS_REGION>"
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
  EOT
}
