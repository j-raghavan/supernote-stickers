# Docker Deployment Guide

This project includes a Docker Compose configuration for easy deployment on TrueNAS, Docker Desktop, or any Docker-compatible system.

## Quick Start

### Prerequisites
- Docker and Docker Compose installed
- (Optional) TrueNAS with Docker/Podman support

### Build and Run

```bash
# Build the image
docker-compose build

# Start the container
docker-compose up -d

# View logs
docker-compose logs -f supernote-stickers

# Stop the container
docker-compose down
```

The web app will be available at `http://localhost:5000`

## TrueNAS Installation via Custom YAML

1. **Clone or copy this repository** to your TrueNAS system
   ```bash
   git clone https://github.com/j-raghavan/supernote-stickers.git
   cd supernote-stickers
   ```

2. **In TrueNAS UI:**
   - Go to **Apps** → **Discover** (or **Custom Apps**)
   - Click **Launch Docker Image** or similar option
   - Choose **Compose Configuration**
   - Paste the contents of `docker-compose.yaml`
   - Adjust settings as needed:
     - **Port Mapping**: Change `5000` if desired
     - **Volumes**: Add persistent storage if needed

3. **Deploy** and access at `http://<truenas-ip>:5000`

## Configuration

Edit `docker-compose.yaml` to customize:

| Setting | Default | Description |
|---------|---------|-------------|
| **Port** | `5000` | Web server port (format: `HOST:CONTAINER`) |
| **Restart Policy** | `unless-stopped` | Restart behavior |
| **Environment** | `FLASK_ENV=production` | Flask configuration |

### Example: Change Port to 8000

```yaml
ports:
  - "8000:5000"  # Access at http://localhost:8000
```

## Notes

- The application is **stateless** – no persistent storage required unless you modify it
- The Docker image uses a **multi-stage build** for smaller size
- Health checks are included to monitor container status
- Logs can be viewed via `docker-compose logs`
