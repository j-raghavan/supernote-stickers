# Publishing Docker Image to Docker Hub or GitHub Packages

This guide explains how to automatically build and publish the Docker image to Docker Hub and/or GitHub Packages.

## Option 1: GitHub Packages (Recommended for GitHub Repos)

### Setup

1. **Enable GitHub Packages:**
   - Go to your repository settings
   - No additional setup needed – you have automatic `GITHUB_TOKEN`

2. **The Action Already Works:**
   - Push code to `master` or create a release tag (`v1.0.0`)
   - GitHub Actions automatically builds and publishes to `ghcr.io`

3. **Access Your Image:**
   ```bash
   docker pull ghcr.io/j-raghavan/supernote-stickers:latest
   docker pull ghcr.io/j-raghavan/supernote-stickers:master
   docker pull ghcr.io/j-raghavan/supernote-stickers:v1.0.0  # if tagged
   ```

## Option 2: Docker Hub

### Initial Setup

1. **Create Docker Hub Account:**
   - Go to [hub.docker.com](https://hub.docker.com)
   - Sign up and create an account

2. **Create an Access Token:**
   - Log in to Docker Hub
   - Account Settings → Security → New Access Token
   - Name it something like `github-actions`
   - Copy the token (you'll use it once)

3. **Add Secrets to GitHub:**
   - Go to your repo → Settings → Secrets and variables → Actions
   - Add two new secrets:
     - `DOCKERHUB_USERNAME`: Your Docker Hub username
     - `DOCKERHUB_TOKEN`: The access token from step 2

4. **Done!** The GitHub Action will now publish to Docker Hub automatically.

5. **Access Your Image:**
   ```bash
   docker pull yourusername/supernote-stickers:latest
   docker pull yourusername/supernote-stickers:master
   docker pull yourusername/supernote-stickers:v1.0.0  # if tagged
   ```

## Automated Publishing

Once set up, the workflow runs on:
- **Any push to `master` branch** → publishes as `latest` and `master` tags
- **Git release tags** (e.g., `v1.0.0`) → publishes with version tags
- **Pull requests** → builds (but doesn't push)

## TrueNAS Setup with Hosted Image

Update your `docker-compose.yaml` to use the hosted image:

### GitHub Packages
```yaml
services:
  supernote-stickers:
    image: ghcr.io/j-raghavan/supernote-stickers:latest
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - FLASK_ENV=production
```

### Docker Hub
```yaml
services:
  supernote-stickers:
    image: yourusername/supernote-stickers:latest
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - FLASK_ENV=production
```

Then on TrueNAS:
- Paste this simplified `docker-compose.yaml` into the custom app
- No Dockerfile needed – just pulls the pre-built image
- Much faster startup!

## Manual Publishing (If Needed)

If you want to manually build and push:

```bash
# Docker Hub
docker build -t yourusername/supernote-stickers:latest .
docker push yourusername/supernote-stickers:latest

# GitHub Packages
docker build -t ghcr.io/j-raghavan/supernote-stickers:latest .
docker login ghcr.io  # Use personal access token as password
docker push ghcr.io/j-raghavan/supernote-stickers:latest
```

## Tagging and Releases

To create a version release:
```bash
git tag v1.0.0
git push origin v1.0.0
```

This triggers a build with additional tags like `v1.0.0` and `1.0`.

## Troubleshooting

- **Action fails to push:** Check that secrets are configured correctly
- **Image not found on pull:** Wait 2-3 minutes after push (build takes time)
- **403 errors on Docker Hub:** Verify token has correct permissions (not limited to specific repos)
