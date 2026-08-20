// PM2 entry for the standalone BLOY Dev Agent service.
//
// Separate from BAM on purpose: restarting BAM must not touch a run in flight.
// Env is loaded from agent-manager/.env so Twenty credentials live in one place.
module.exports = {
  apps: [
    {
      name: 'bloy-dev-agent',
      cwd: '/home/bss-group/BLOY/agent-manager',
      script: 'uv',
      args: 'run python -m bloy_dev_agent.service',
      interpreter: 'none',
      autorestart: true,
      max_restarts: 10,
      env: {
        PYTHONPATH: '/home/bss-group/BLOY/agent-manager/community_plugins',
        PYTHONUNBUFFERED: '1',
        BLOY_AGENT_PORT: '8100',
        BAM_URL: 'http://localhost:8000',
      },
    },
    {
      // Deliberately a SEPARATE process from bloy-dev-agent above, even
      // though it is the same repo — this is the only surface an opted-in
      // sandbox is granted network access to reach (see
      // features/sandbox_runner.py's STAGING_EGRESS_ALLOW), so a slow or
      // stuck deploy here must never be able to block the dashboard or the
      // Twenty poll loop on :8100.
      name: 'bloy-staging-control',
      cwd: '/home/bss-group/BLOY/agent-manager',
      script: 'uv',
      args: 'run python -m bloy_dev_agent.staging_control.service',
      interpreter: 'none',
      autorestart: true,
      max_restarts: 10,
      env: {
        PYTHONPATH: '/home/bss-group/BLOY/agent-manager/community_plugins',
        PYTHONUNBUFFERED: '1',
        // Docker bridge gateway, not loopback and not 0.0.0.0 — reachable
        // from a sandbox on the default bridge network, unreachable from the
        // real network. See staging_control/service.py's module docstring.
        BLOY_STAGING_CONTROL_HOST: '172.17.0.1',
        BLOY_STAGING_CONTROL_PORT: '8110',
      },
    },
  ],
};
