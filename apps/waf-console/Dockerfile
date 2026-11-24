FROM golang:1.23 AS build
WORKDIR /src
COPY go.mod go.sum* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/waf-console ./cmd/waf-console

FROM gcr.io/distroless/static:nonroot
COPY --from=build /out/waf-console /waf-console
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/waf-console"]
