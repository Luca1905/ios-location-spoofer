# Build notes

## Go archive linking

The tunnel target links Go through `-lgolocationspoofer`, with its library search
path pointing to `GoSpoofer/build` for physical devices and
`GoSpoofer/build/iphonesimulator` for simulators. Do not also add the archive to
Copy Bundle Resources or link the physical-device archive directly in the
Frameworks phase: that prevents a simulator build from selecting its own archive.

Build the appropriate archive before Xcode:

```sh
bash GoSpoofer/make.sh              # physical iPhone
bash GoSpoofer/make.sh --simulator  # Apple Silicon simulator
```

The physical-device symlink and generated header remain unchanged when building
for the simulator. `project.yml` includes the conditional library search path,
so regenerating the project retains the correct archive selection.
