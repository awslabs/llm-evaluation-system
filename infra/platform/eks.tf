# EKS creates a "cluster security group" automatically that is NOT managed by
# Terraform. On destroy, this SG can be left orphaned in the VPC, blocking VPC
# deletion. module.eks depends on this so destroy order is: EKS first, cleanup after.
resource "null_resource" "eks_managed_sg_cleanup" {
  triggers = {
    vpc_id       = var.vpc_id
    cluster_name = local.name
    region       = var.region
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      for i in $(seq 1 18); do
        SGs=$(aws ec2 describe-security-groups \
          --filters "Name=vpc-id,Values=${self.triggers.vpc_id}" \
                    "Name=tag:aws:eks:cluster-name,Values=${self.triggers.cluster_name}" \
          --query 'SecurityGroups[].GroupId' --output text \
          --region ${self.triggers.region} 2>/dev/null)
        [ -z "$SGs" ] && echo "No EKS-managed security groups remaining." && exit 0
        for sg in $SGs; do
          echo "Deleting EKS-managed security group: $sg"
          if aws ec2 delete-security-group --group-id $sg --region ${self.triggers.region} 2>/dev/null; then
            echo "Deleted $sg"
          else
            echo "Retrying $sg... ($i/18)"
          fi
        done
        sleep 10
      done
      echo "WARNING: EKS-managed security groups may not be fully cleaned up"
    EOT
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.name
  cluster_version = var.eks_cluster_version

  cluster_endpoint_public_access  = true
  cluster_endpoint_private_access = true

  # Control plane logging
  cluster_enabled_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  vpc_id     = var.vpc_id
  subnet_ids = var.private_subnets

  # EKS Addons
  cluster_addons = {
    coredns                = { most_recent = true }
    kube-proxy             = { most_recent = true }
    vpc-cni                = { most_recent = true }
    eks-pod-identity-agent = { most_recent = true }
    metrics-server         = { most_recent = true } # Required for HPA to work
    amazon-cloudwatch-observability = { most_recent = true }
    aws-ebs-csi-driver             = { most_recent = true }
  }

  # Access
  enable_cluster_creator_admin_permissions = true
  authentication_mode                      = "API"

  access_entries = {
    for idx, arn in var.cluster_admin_role_arns : "admin-${idx}" => {
      principal_arn = arn
      policy_associations = {
        admin = {
          policy_arn   = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = { type = "cluster" }
        }
      }
    }
  }

  # Minimal node group for Karpenter + addons
  eks_managed_node_groups = {
    karpenter = {
      ami_type       = "AL2023_ARM_64_STANDARD"
      instance_types = ["t4g.medium"]

      min_size     = 2
      max_size     = 3
      desired_size = 2

      # Auto-replace unhealthy (NotReady) nodes
      node_repair_config = {
        enabled = true
      }
    }
  }

  # Allow control plane to reach metrics-server (port 10251)
  node_security_group_additional_rules = {
    ingress_metrics_server = {
      description                   = "Control plane to metrics-server"
      protocol                      = "tcp"
      from_port                     = 10251
      to_port                       = 10251
      type                          = "ingress"
      source_cluster_security_group = true
    }
  }

  # Allow Karpenter nodes to join
  node_security_group_tags = {
    "karpenter.sh/discovery" = local.name
  }

  tags = {
    "karpenter.sh/discovery" = local.name
  }

  depends_on = [null_resource.eks_managed_sg_cleanup]
}

#------------------------------------------------------------------------------
# Cluster Readiness Check
#------------------------------------------------------------------------------

resource "null_resource" "wait_for_cluster" {
  depends_on = [module.eks]

  provisioner "local-exec" {
    command = "until curl -sk $ENDPOINT/healthz >/dev/null 2>&1; do echo 'Waiting for EKS cluster...'; sleep 5; done"
    environment = {
      ENDPOINT = module.eks.cluster_endpoint
    }
  }
}

#------------------------------------------------------------------------------
# Karpenter
#------------------------------------------------------------------------------

module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 20.0"

  cluster_name = module.eks.cluster_name

  enable_v1_permissions           = true
  enable_pod_identity             = true
  create_pod_identity_association = true

  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }
}

resource "helm_release" "karpenter" {
  namespace           = "kube-system"
  name                = "karpenter"
  repository          = "oci://public.ecr.aws/karpenter"
  repository_username = data.aws_ecrpublic_authorization_token.token.user_name
  repository_password = data.aws_ecrpublic_authorization_token.token.password
  chart               = "karpenter"
  version             = "1.0.12"
  wait                = true
  timeout             = 300

  values = [
    <<-EOT
    settings:
      clusterName: ${module.eks.cluster_name}
      clusterEndpoint: ${module.eks.cluster_endpoint}
    serviceAccount:
      name: ${module.karpenter.service_account}
    EOT
  ]

  depends_on = [module.karpenter, null_resource.wait_for_cluster, helm_release.alb_controller, module.eks]
}

resource "kubectl_manifest" "karpenter_node_class" {
  yaml_body = <<-YAML
    apiVersion: karpenter.k8s.aws/v1
    kind: EC2NodeClass
    metadata:
      name: default
    spec:
      role: ${module.karpenter.node_iam_role_name}
      amiSelectorTerms:
        - alias: al2023@v20260114
      subnetSelectorTerms:
        - tags:
            karpenter.sh/discovery: ${local.name}
      securityGroupSelectorTerms:
        - tags:
            karpenter.sh/discovery: ${local.name}
      tags:
        karpenter.sh/discovery: ${local.name}
  YAML

  depends_on = [helm_release.karpenter, module.eks]
}

resource "kubectl_manifest" "karpenter_node_pool" {
  yaml_body = <<-YAML
    apiVersion: karpenter.sh/v1
    kind: NodePool
    metadata:
      name: default
    spec:
      template:
        spec:
          nodeClassRef:
            group: karpenter.k8s.aws
            kind: EC2NodeClass
            name: default
          requirements:
            - key: kubernetes.io/arch
              operator: In
              values: ["arm64"]
            - key: karpenter.sh/capacity-type
              operator: In
              values: ["on-demand"]
            - key: karpenter.k8s.aws/instance-category
              operator: In
              values: ["t"]
            - key: karpenter.k8s.aws/instance-size
              operator: In
              values: ["medium", "large", "xlarge"]
      limits:
        cpu: 100
        memory: 200Gi
      disruption:
        consolidationPolicy: WhenEmptyOrUnderutilized
        consolidateAfter: 1m
  YAML

  depends_on = [kubectl_manifest.karpenter_node_class, module.eks]
}

# Karpenter creates EC2 instances outside of Terraform. On destroy, NodePool
# deletion triggers async instance termination — this resource waits for those
# instances to fully terminate before Terraform deletes security groups.
resource "null_resource" "karpenter_node_cleanup" {
  triggers = {
    cluster_name = local.name
    region       = var.region
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      for i in $(seq 1 30); do
        IDS=$(aws ec2 describe-instances \
          --filters "Name=tag:karpenter.sh/discovery,Values=${self.triggers.cluster_name}" \
                    "Name=instance-state-name,Values=running,shutting-down,stopping" \
          --region ${self.triggers.region} \
          --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null)
        [ -z "$IDS" ] && echo "All Karpenter nodes terminated." && exit 0
        if [ "$i" -eq 1 ]; then
          echo "Terminating Karpenter nodes: $IDS"
          aws ec2 terminate-instances --instance-ids $IDS --region ${self.triggers.region} >/dev/null 2>&1 || true
        fi
        echo "Waiting for Karpenter nodes to terminate... ($i/30)"
        sleep 10
      done
      echo "WARNING: Karpenter nodes may not be fully terminated"
    EOT
  }

  depends_on = [kubectl_manifest.karpenter_node_pool]
}

#------------------------------------------------------------------------------
# EBS CSI Driver Pod Identity
#------------------------------------------------------------------------------

module "ebs_csi_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 1.0"

  name = "${local.name}-ebs-csi"

  attach_aws_ebs_csi_policy = true

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "ebs-csi-controller-sa"
    }
  }
}

#------------------------------------------------------------------------------
# CloudWatch Observability Pod Identity
#------------------------------------------------------------------------------

module "cloudwatch_observability_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 1.0"

  name = "${local.name}-cloudwatch-observability"

  attach_aws_cloudwatch_observability_policy = true

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "amazon-cloudwatch"
      service_account = "cloudwatch-agent"
    }
  }
}

#------------------------------------------------------------------------------
# AWS Load Balancer Controller (via Helm - more control than addon)
#------------------------------------------------------------------------------

module "alb_controller_pod_identity" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 1.0"

  name = "${local.name}-alb-controller"

  attach_aws_lb_controller_policy = true

  associations = {
    main = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
    }
  }
}

resource "helm_release" "alb_controller" {
  namespace     = "kube-system"
  name          = "aws-load-balancer-controller"
  repository    = "https://aws.github.io/eks-charts"
  chart         = "aws-load-balancer-controller"
  version       = "1.17.1"
  wait          = true
  wait_for_jobs = true
  timeout       = 600

  # EKS metrics API extension has malformed OpenAPI schema - skip client-side validation
  disable_openapi_validation = true

  set {
    name  = "clusterName"
    value = module.eks.cluster_name
  }
  set {
    name  = "serviceAccount.name"
    value = "aws-load-balancer-controller"
  }
  set {
    name  = "vpcId"
    value = var.vpc_id
  }

  depends_on = [module.alb_controller_pod_identity, null_resource.wait_for_cluster, module.eks]
}
