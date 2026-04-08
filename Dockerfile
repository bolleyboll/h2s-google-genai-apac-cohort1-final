# Python image to use.
FROM astral/uv:0.11.3-python3.13-alpine

# Set the working directory to /app
WORKDIR /app

RUN apk add --no-cache nodejs npm

# copy the requirements file used for dependencies
COPY pyproject.toml uv.lock ./

# Install any needed packages specified in requirements.txt
RUN uv sync

# Copy the rest of the working directory contents into the container at /app
COPY . .

EXPOSE 8080

# Run main.py when the container launches
ENTRYPOINT ["uv", "run", "main.py"]
