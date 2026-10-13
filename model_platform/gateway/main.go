package main

import (
	"log"
	"os"

	gatewayapp "model-platform/gateway/app"
	"model-platform/gateway/grpcclient"
	"model-platform/internal/datasets"

	"github.com/gofiber/fiber/v3"
)

func main() {
	clients, err := grpcclient.Connect(env("MANAGEMENT_GRPC_ADDRESS", "management:9000"), env("OPERATION_GRPC_ADDRESS", "operation:9000"), env("TRAINING_GRPC_ADDRESS", "training:9000"), env("DEPLOYMENT_GRPC_ADDRESS", "deployment:9000"))
	if err != nil {
		log.Fatal(err)
	}
	defer clients.Close()
	app := fiber.New(fiber.Config{AppName: "Model Platform Gateway", BodyLimit: datasets.MaxBytes + (1 << 20)})
	gatewayapp.New(clients).Register(app)
	address := env("HTTP_ADDRESS", ":8000")
	log.Printf("gateway REST listening on %s", address)
	log.Fatal(app.Listen(address))
}
func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
