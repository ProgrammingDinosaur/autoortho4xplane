try:
    from autoortho.mount_worker import main
except ImportError:
    from mount_worker import main


if __name__ == "__main__":
    main()
