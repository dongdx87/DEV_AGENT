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
  ],
};
