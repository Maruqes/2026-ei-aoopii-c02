CREATE TABLE IF NOT EXISTS users (
    id INT PRIMARY KEY AUTO_INCREMENT,
    discord_id VARCHAR(255) NOT NULL UNIQUE,
    username VARCHAR(255) NOT NULL,
    display_name VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS messages (
    id INT PRIMARY KEY AUTO_INCREMENT,
    user_id INT NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    channel_name VARCHAR(255),
    content TEXT NOT NULL,
    tstamp DATETIME NOT NULL,

    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS text_chunks (
    id INT PRIMARY KEY AUTO_INCREMENT,
    channel_name VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    start_at DATETIME,
    end_at DATETIME
);
