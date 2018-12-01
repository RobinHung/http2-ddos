const spdy = require("spdy");
const express = require("express");
const path = require("path");
const fs = require("fs");
const port = 3000;

const app = express();

const options = {
  key: fs.readFileSync(__dirname + "/server.key"),
  cert: fs.readFileSync(__dirname + "/server.crt")
};

app.get("*", (req, res) => {
  res.status(200).json({ message: "Hello HTTP/2!" });
});

spdy.createServer(options, app).listen(port, error => {
  if (error) {
    console.error(error);
    return process.exit(1);
  } else {
    console.log("Listening on port: " + port + ".");
  }
});
