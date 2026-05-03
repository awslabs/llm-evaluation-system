#------------------------------------------------------------------------------
# Agent Sandbox Security: Cilium (network policies) + gVisor (kernel isolation)
#
# Cilium: CNI chaining mode alongside VPC CNI. Provides CiliumNetworkPolicy
# CRDs required by inspect-k8s-sandbox's built-in Helm chart.
#
# gVisor: DaemonSet installs runsc on nodes + RuntimeClass. Provides
# user-space kernel isolation for agent pods.
#------------------------------------------------------------------------------

#------------------------------------------------------------------------------
# Cilium — CNI chaining with AWS VPC CNI (v1.19.x)
# Ref: https://docs.cilium.io/en/stable/installation/cni-chaining-aws-cni/
#------------------------------------------------------------------------------

resource "helm_release" "cilium" {
  namespace     = "kube-system"
  name          = "cilium"
  repository    = "https://helm.cilium.io/"
  chart         = "cilium"
  version       = "1.19.3"
  wait          = true
  wait_for_jobs = true
  timeout       = 600

  # CNI chaining: Cilium chains on top of VPC CNI (which handles IP allocation)
  set {
    name  = "cni.chainingMode"
    value = "aws-cni"
  }
  # Do not remove VPC CNI config from /etc/cni/net.d
  set {
    name  = "cni.exclusive"
    value = "false"
  }
  # VPC CNI handles masquerade
  set {
    name  = "enableIPv4Masquerade"
    value = "false"
  }
  # No tunnel — VPC CNI handles routing
  set {
    name  = "routingMode"
    value = "native"
  }

  depends_on = [null_resource.wait_for_cluster, module.eks]
}

#------------------------------------------------------------------------------
# gVisor — DaemonSet installer + RuntimeClass
# Installs runsc binary and containerd-shim-runsc-v1 on each node,
# configures containerd, and restarts it.
# Handles both x86_64 and ARM64, containerd v1 and v2 config schemas.
#------------------------------------------------------------------------------

resource "kubernetes_runtime_class_v1" "gvisor" {
  metadata {
    name = "gvisor"
  }
  handler = "runsc"

  depends_on = [null_resource.wait_for_cluster, module.eks]
}

resource "kubernetes_daemon_set_v1" "gvisor_installer" {
  metadata {
    name      = "gvisor-installer"
    namespace = "kube-system"
    labels = {
      app = "gvisor-installer"
    }
  }

  spec {
    selector {
      match_labels = {
        app = "gvisor-installer"
      }
    }

    template {
      metadata {
        labels = {
          app = "gvisor-installer"
        }
      }

      spec {
        host_pid     = true
        host_network = true

        init_container {
          name  = "install-gvisor"
          image = "public.ecr.aws/ubuntu/ubuntu:24.04"

          command = ["/bin/bash", "-c"]
          args = [<<-EOT
            set -euo pipefail

            ARCH=$(uname -m)
            if [ "$ARCH" = "x86_64" ]; then
              ARCH="x86_64"
            elif [ "$ARCH" = "aarch64" ]; then
              ARCH="aarch64"
            fi

            # Check if already installed
            if /host/usr/local/bin/runsc --version 2>/dev/null; then
              echo "gVisor already installed"
              exit 0
            fi

            echo "Installing gVisor for $ARCH..."
            apt-get update && apt-get install -y curl
            curl -fsSL "https://storage.googleapis.com/gvisor/releases/release/latest/$ARCH/runsc" -o /host/usr/local/bin/runsc
            curl -fsSL "https://storage.googleapis.com/gvisor/releases/release/latest/$ARCH/containerd-shim-runsc-v1" -o /host/usr/local/bin/containerd-shim-runsc-v1
            chmod 555 /host/usr/local/bin/runsc /host/usr/local/bin/containerd-shim-runsc-v1
            echo "gVisor binaries installed"

            # Configure containerd to use runsc runtime
            CONFIG_FILE="/host/etc/containerd/config.toml"

            if grep -q "containerd.runtimes.runsc" "$CONFIG_FILE" 2>/dev/null; then
              echo "runsc runtime already configured in containerd"
              exit 0
            fi

            # Detect containerd config version and append runtime config
            if grep -q 'io.containerd.cri.v1.runtime' "$CONFIG_FILE" 2>/dev/null; then
              # Containerd v2 (config schema v3)
              cat >> "$CONFIG_FILE" <<'TOML'

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"

[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runsc.options]
  TypeUrl = "io.containerd.runsc.v1.options"
TOML
            else
              # Containerd v1 (config schema v2)
              cat >> "$CONFIG_FILE" <<'TOML'

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
TOML
            fi

            echo "runsc runtime added to containerd config"
            # Restart containerd on host
            nsenter --target 1 --mount --uts --ipc --net --pid -- systemctl restart containerd
            echo "containerd restarted with runsc runtime"
          EOT
          ]

          security_context {
            privileged = true
          }

          volume_mount {
            name       = "host"
            mount_path = "/host"
          }
        }

        container {
          name  = "pause"
          image = "registry.k8s.io/pause:3.9"
        }

        volume {
          name = "host"
          host_path {
            path = "/"
          }
        }

        toleration {
          operator = "Exists"
        }
      }
    }
  }

  depends_on = [null_resource.wait_for_cluster, module.eks]
}

#------------------------------------------------------------------------------
# Network Policy — isolate agent pods
# Blocks: IMDS (169.254.169.254), cluster-internal traffic (10.0.0.0/8)
# Allows: DNS + internet
# Targets pods with label inspectSandbox=true (set by inspect-k8s-sandbox)
#------------------------------------------------------------------------------

resource "kubernetes_network_policy_v1" "agent_pods_deny_internal" {
  metadata {
    name      = "agent-pods-deny-internal"
    namespace = kubernetes_namespace.app.metadata[0].name
  }

  spec {
    pod_selector {
      match_labels = {
        "inspectSandbox" = "true"
      }
    }

    policy_types = ["Egress"]

    egress {
      # Allow DNS
      to {
        namespace_selector {}
        pod_selector {
          match_labels = {
            "k8s-app" = "kube-dns"
          }
        }
      }
      ports {
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
    }

    egress {
      # Allow internet but block cluster-internal + IMDS
      to {
        ip_block {
          cidr = "0.0.0.0/0"
          except = [
            "10.0.0.0/8",
            "172.16.0.0/12",
            "169.254.169.254/32",
          ]
        }
      }
    }
  }

  depends_on = [kubernetes_namespace.app]
}
