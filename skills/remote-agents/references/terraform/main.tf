data "aws_ami" "ubuntu" {
  count       = var.ami_id == null ? 1 : 0
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

locals {
  ami_id = var.ami_id != null ? var.ami_id : data.aws_ami.ubuntu[0].id
  tags = merge(var.tags, {
    Name      = var.instance_name
    Component = "remote-agents"
  })
}

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name_prefix        = "${var.instance_name}-"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "this" {
  name_prefix = "${var.instance_name}-"
  role        = aws_iam_role.this.name
  tags        = local.tags
}

resource "aws_security_group" "this" {
  name_prefix = "${var.instance_name}-"
  description = "No ingress; management through AWS Systems Manager"
  vpc_id      = var.vpc_id

  # Deliberately no ingress blocks.
  egress {
    description = "Bootstrap and agent egress; narrow this for your environment"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

resource "aws_key_pair" "this" {
  key_name_prefix = "${var.instance_name}-"
  public_key      = var.ssh_public_key
  tags            = local.tags
}

resource "aws_instance" "this" {
  depends_on = [aws_iam_role_policy_attachment.ssm]

  ami                         = local.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  associate_public_ip_address = var.associate_public_ip_address
  vpc_security_group_ids      = [aws_security_group.this.id]
  iam_instance_profile        = aws_iam_instance_profile.this.name
  key_name                    = aws_key_pair.this.key_name
  user_data                   = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    bootstrap_commands = var.bootstrap_commands
  })
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = var.root_volume_gib
    delete_on_termination = true
  }

  tags = local.tags
}
