variable "vpc_id" {
  description = "Existing VPC ID."
  type        = string
}

variable "subnet_id" {
  description = "Existing subnet ID. Prefer a private subnet with NAT/proxy egress."
  type        = string
}

variable "ssh_public_key" {
  description = "OpenSSH public key used for SSH-over-SSM."
  type        = string
  sensitive   = true
}

variable "ami_id" {
  description = "Optional AMI override. Null selects the latest Canonical Ubuntu 24.04 amd64 image."
  type        = string
  default     = null
}

variable "instance_name" {
  description = "Name tag and resource prefix."
  type        = string
  default     = "remote-agent"
}

variable "instance_type" {
  description = "EC2 instance type. Adjust to repository build workload."
  type        = string
  default     = "m7i.2xlarge"
}

variable "root_volume_gib" {
  description = "Encrypted gp3 root volume size."
  type        = number
  default     = 150
}

variable "associate_public_ip_address" {
  description = "Keep false for the recommended SSM-only private deployment."
  type        = bool
  default     = false
}

variable "bootstrap_commands" {
  description = "Extra cloud-init runcmd entries (agent CLIs, toolchains) run as root at first boot."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
