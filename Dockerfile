# Python image to use.
FROM astral/uv:0.11.3-python3.13-alpine

# Set the working directory to /app
WORKDIR /app

RUN apk add --no-cache nodejs npm

# copy the requirements file used for dependencies
COPY pyproject.toml uv.lock ./

# Install any needed packages specified in requirements.txt
RUN uv sync

# Cache npm install separately from app source so source-only edits don't
# re-run dependency installs.
COPY frontend/package.json frontend/package-lock.json* frontend/
RUN cd frontend && npm install --no-audit --no-fund --prefer-offline

# Copy the rest of the working directory contents into the container at /app
COPY . .

# Build the Vue/Vite SPA to static/dist/ which Flask serves as the SPA entry.
RUN cd frontend && npm run build

EXPOSE 8080

# Run main.py when the container launches
ENTRYPOINT ["uv", "run", "main.py"]
